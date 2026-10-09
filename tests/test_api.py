from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import Mock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.providers.base import Capabilities, ProviderError
from app.providers.registry import Registry
from app.service import Service
from tests.fakes import FakeProvider


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "service.sqlite"),
        api_token="integration-test-token-with-enough-entropy",
        encryption_key=Fernet.generate_key().decode(),
        sync_interval_seconds=1,
    )


@pytest.fixture
def providers(tmp_path):
    path = str(tmp_path / "upstream.sqlite")
    return [FakeProvider("fake_a", path), FakeProvider("fake_b", path)]


def app_client(settings, providers):
    return TestClient(create_app(settings, Registry(providers)), headers={"Authorization": f"Bearer {settings.api_token}"})


@pytest.fixture
def client(settings, providers):
    with app_client(settings, providers) as client:
        yield client


def operation(client, operation_id):
    response = client.get(f"/v1/operations/{operation_id}")
    assert response.status_code == 200, response.text
    return response.json()


def create_mailbox(client, provider="fake_a", **fields):
    response = client.post("/v1/mailboxes", json={"provider": provider, **fields}, headers={"Idempotency-Key": uuid4().hex})
    assert response.status_code == 202, response.text
    client.app.state.service.run_once()
    result = operation(client, response.json()["id"])
    assert result["status"] == "succeeded", result
    response = client.get(f"/v1/mailboxes/{result['result']['mailbox_id']}")
    assert response.status_code == 200, response.text
    return response.json()


def send_message(client, mailbox, recipient, *, key=None, text="A private test message"):
    return client.post(
        f"/v1/mailboxes/{mailbox['id']}/messages",
        json={"recipients": [recipient], "subject": "Integration test", "text": text},
        headers={"Idempotency-Key": key or uuid4().hex},
    )


def error_code(response):
    return response.json()["error"]["code"]


def test_authentication_and_public_health(settings, providers):
    with TestClient(create_app(settings, Registry(providers))) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        assert client.get("/v1/capabilities").status_code == 401
        assert client.get("/v1/mailboxes", headers={"Authorization": "Bearer wrong-token"}).status_code == 401
        assert client.post("/v1/mailboxes", json={}).status_code == 401


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_operation_history_requires_authentication(settings, providers, authorization):
    with TestClient(create_app(settings, Registry(providers))) as client:
        headers = {"Authorization": authorization} if authorization else {}
        response = client.get("/v1/operations", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "invalid"}])
def test_operation_history_validates_pagination(client, params):
    response = client.get("/v1/operations", params=params)
    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"


def test_operation_history_is_ordered_paginated_and_owner_scoped(client):
    assert client.get("/v1/operations").json() == {"items": [], "limit": 50, "offset": 0, "total": 0}
    payload = {"provider": "fake_a", "required_capabilities": ["receive"], "ttl_seconds": 3600}
    service = client.app.state.service
    operations = [service.submit("create", f"history-{index}", payload) for index in range(4)]
    with service.db.connect(write=True) as conn:
        conn.execute("UPDATE operations SET created_at='2026-01-01T00:00:00+00:00'")
        conn.execute("UPDATE operations SET created_at='2026-01-02T00:00:00+00:00' WHERE id=?", (operations[0]["id"],))
        conn.execute("UPDATE operations SET owner_id='another-owner' WHERE id=?", (operations[3]["id"],))
    expected = [operations[0]["id"], *sorted([operations[1]["id"], operations[2]["id"]], reverse=True)]
    first = client.get("/v1/operations", params={"limit": 2}).json()
    second = client.get("/v1/operations", params={"limit": 2, "offset": 2}).json()
    assert (first["limit"], first["offset"], first["total"]) == (2, 0, 3)
    assert (second["limit"], second["offset"], second["total"]) == (2, 2, 3)
    assert [item["id"] for item in first["items"] + second["items"]] == expected
    assert client.get("/v1/operations", params={"offset": 10}).json() == {"items": [], "limit": 50, "offset": 10, "total": 3}
    assert client.get(f"/v1/operations/{operations[3]['id']}").status_code == 404


def test_operation_history_survives_app_restart_without_disclosing_payloads(settings, providers):
    with app_client(settings, providers) as client:
        mailbox = create_mailbox(client)
        sent = send_message(client, mailbox, mailbox["email"], key="private-idempotency-key", text="private-mail-body")
        assert sent.status_code == 202
        client.app.state.service.run_once()
        deleted = client.delete(f"/v1/mailboxes/{mailbox['id']}", headers={"Idempotency-Key": "delete-history"})
        assert deleted.status_code == 202
        client.app.state.service.run_once()
        before = client.get("/v1/operations").json()
        assert before["total"] == 3
        assert {item["kind"] for item in before["items"]} == {"create", "send", "delete"}
        assert all(item["status"] == "succeeded" for item in before["items"])
    with app_client(settings, providers) as restarted:
        response = restarted.get("/v1/operations")
        assert response.status_code == 200
        assert response.json() == before
        assert "private-idempotency-key" not in response.text
        assert "private-mail-body" not in response.text
        for item in response.json()["items"]:
            assert set(item) == {"id", "kind", "status", "provider_id", "mailbox_id", "result", "error_code", "created_at", "updated_at"}
            assert item == operation(restarted, item["id"])


def test_default_ttl_and_capability_routing(settings, tmp_path):
    path = str(tmp_path / "upstream.sqlite")
    readonly = FakeProvider("readonly", path, can_send=False)
    sender = FakeProvider("sender", path)
    with app_client(settings, [readonly, sender]) as client:
        receiving = create_mailbox(client, provider="auto")
        assert receiving["provider_id"] == "readonly"
        lifetime = datetime.fromisoformat(receiving["expires_at"]) - datetime.fromisoformat(receiving["created_at"])
        assert lifetime.total_seconds() == pytest.approx(3600, abs=1)
        sending = create_mailbox(client, provider="auto", required_capabilities=["receive", "send"])
        assert sending["provider_id"] == "sender"
        rejected = send_message(client, receiving, sending["email"])
        assert rejected.status_code == 422
        assert error_code(rejected) == "CAPABILITY_UNSUPPORTED"


def test_creation_idempotency_replays_and_rejects_payload_changes(client):
    headers = {"Idempotency-Key": "create-once"}
    first = client.post("/v1/mailboxes", json={"provider": "fake_a"}, headers=headers)
    assert first.status_code == 202
    replay = client.post("/v1/mailboxes", json={"provider": "fake_a"}, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]
    conflict = client.post("/v1/mailboxes", json={"provider": "fake_b"}, headers=headers)
    assert conflict.status_code == 409
    assert error_code(conflict) == "IDEMPOTENCY_CONFLICT"
    client.app.state.service.run_once()
    replay = client.post("/v1/mailboxes", json={"provider": "fake_a"}, headers=headers)
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["status"] == "succeeded"
    assert len(client.get("/v1/mailboxes").json()["items"]) == 1


def test_cross_provider_delivery_deduplication_and_message_detail(client):
    source = create_mailbox(client, "fake_a")
    target = create_mailbox(client, "fake_b")
    response = send_message(client, source, target["email"], key="send-once")
    assert response.status_code == 202, response.text
    client.app.state.service.run_once()
    completed = operation(client, response.json()["id"])
    assert completed["status"] == "succeeded"
    assert completed["provider_id"] == "fake_a"
    replay = send_message(client, source, target["email"], key="send-once")
    assert replay.status_code == 202
    assert replay.json()["id"] == completed["id"]
    client.app.state.service.run_once()
    client.app.state.service.sync_mailbox(target["id"])
    client.app.state.service.sync_mailbox(target["id"])
    page = client.get(f"/v1/mailboxes/{target['id']}/messages").json()
    assert page["last_synced_at"] is not None
    assert len(page["items"]) == 1
    message = page["items"][0]
    assert message["sender"] == source["email"]
    assert "text" not in message
    detail = client.get(f"/v1/mailboxes/{target['id']}/messages/{message['id']}")
    assert detail.status_code == 200
    assert detail.json()["text"] == "A private test message"
    wrong_mailbox = client.get(f"/v1/mailboxes/{source['id']}/messages/{message['id']}")
    assert wrong_mailbox.status_code == 404


def test_binding_survives_restart_and_provider_order_changes(settings, providers):
    with app_client(settings, providers) as client:
        source = create_mailbox(client, "fake_a")
        target = create_mailbox(client, "fake_b")
    # Fresh adapters and reversed priority reproduce an API/worker restart and configuration change.
    restarted = [FakeProvider(provider.id, provider.db_path) for provider in reversed(providers)]
    with app_client(settings, restarted) as client:
        mailbox = client.get(f"/v1/mailboxes/{source['id']}").json()
        assert mailbox["email"] == source["email"]
        assert mailbox["provider_id"] == "fake_a"
        lookup = client.get("/v1/mailboxes", params={"email": source["email"]}).json()["items"]
        assert [item["id"] for item in lookup] == [source["id"]]
        assert lookup[0]["provider_id"] == "fake_a"
        response = send_message(client, source, target["email"])
        assert response.status_code == 202, response.text
        assert response.json()["provider_id"] == "fake_a"
        client.app.state.service.run_once()
        assert operation(client, response.json()["id"])["status"] == "succeeded"
        client.app.state.service.sync_mailbox(target["id"])
        messages = client.get(f"/v1/mailboxes/{target['id']}/messages").json()["items"]
        assert len(messages) == 1
        assert messages[0]["sender"] == source["email"]


def test_missing_bound_provider_never_falls_back(settings, providers):
    with app_client(settings, providers) as client:
        source = create_mailbox(client, "fake_a")
        target = create_mailbox(client, "fake_b")
    with app_client(settings, [providers[1]]) as client:
        response = send_message(client, source, target["email"])
        assert response.status_code == 503
        assert error_code(response) == "PROVIDER_UNAVAILABLE"
        client.app.state.service.run_once()
        assert client.get(f"/v1/mailboxes/{target['id']}/messages").json()["items"] == []


def test_mailbox_pages_include_total_with_stable_order_and_owner_scope(client):
    assert client.get("/v1/mailboxes").json() == {"items": [], "limit": 50, "offset": 0, "total": 0}
    mailboxes = [create_mailbox(client) for _ in range(11)]
    with client.app.state.service.db.connect(write=True) as conn:
        conn.execute("UPDATE mailboxes SET created_at='2026-01-01T00:00:00+00:00'")
        conn.execute("UPDATE mailboxes SET created_at='2026-01-02T00:00:00+00:00' WHERE id=?", (mailboxes[0]["id"],))
        conn.execute("UPDATE mailboxes SET owner_id='another-owner' WHERE id=?", (mailboxes[-1]["id"],))
    first = client.get("/v1/mailboxes", params={"limit": 5}).json()
    second = client.get("/v1/mailboxes", params={"limit": 5, "offset": 5}).json()
    expected = [mailboxes[0]["id"], *sorted([mailbox["id"] for mailbox in mailboxes[1:-1]], reverse=True)]
    assert (first["limit"], first["offset"], first["total"], len(first["items"])) == (5, 0, 10, 5)
    assert (second["limit"], second["offset"], second["total"], len(second["items"])) == (5, 5, 10, 5)
    assert [mailbox["id"] for mailbox in first["items"] + second["items"]] == expected
    assert client.get("/v1/mailboxes", params={"limit": 5, "offset": 10}).json() == {"items": [], "limit": 5, "offset": 10, "total": 10}


def test_email_lookup_matches_exact_address(client):
    source = create_mailbox(client, "fake_a")
    other = create_mailbox(client, "fake_b")
    hidden = create_mailbox(client, "fake_a")
    with client.app.state.service.db.connect(write=True) as conn:
        conn.execute("UPDATE mailboxes SET owner_id='another-owner', email=? WHERE id=?", (source["email"], hidden["id"]))
    result = client.get("/v1/mailboxes", params={"email": source["email"]})
    assert result.status_code == 200
    assert [mailbox["id"] for mailbox in result.json()["items"]] == [source["id"]]
    assert result.json()["total"] == 1
    assert client.get("/v1/mailboxes", params={"email": source["email"], "offset": 1}).json() == {
        "items": [],
        "limit": 50,
        "offset": 1,
        "total": 1,
    }
    for address in (source["email"].split("@")[0], "%", "missing@fake-a.test"):
        assert client.get("/v1/mailboxes", params={"email": address}).json() == {"items": [], "limit": 50, "offset": 0, "total": 0}
    assert [mailbox["id"] for mailbox in client.get("/v1/mailboxes", params={"email": other["email"]}).json()["items"]] == [other["id"]]


def test_concurrent_idempotent_creation_queues_only_one_operation(client):
    service = client.app.state.service
    payload = {"provider": "fake_a", "required_capabilities": ["receive"], "ttl_seconds": 3600}

    def submit(_):
        return service.submit("create", "concurrent-create", payload)["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        operation_ids = list(pool.map(submit, range(16)))
    assert len(set(operation_ids)) == 1
    with service.db.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM operations WHERE status='pending'").fetchone()[0] == 1
    service.run_once()
    service.run_once()
    assert operation(client, operation_ids[0])["status"] == "succeeded"
    assert len(client.get("/v1/mailboxes").json()["items"]) == 1


def test_expired_mailbox_remains_readable_while_worker_revokes_upstream_credentials(client):
    mailbox = create_mailbox(client)
    response = send_message(client, mailbox, mailbox["email"])
    assert response.status_code == 202
    service = client.app.state.service
    service.run_once()
    service.sync_mailbox(mailbox["id"])
    page = client.get(f"/v1/mailboxes/{mailbox['id']}/messages").json()
    assert len(page["items"]) == 1
    message_path = f"/v1/mailboxes/{mailbox['id']}/messages/{page['items'][0]['id']}"
    message = client.get(message_path).json()
    with service.db.connect(write=True) as db:
        db.execute("UPDATE mailboxes SET expires_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", mailbox["id"]))
    assert client.get(f"/v1/mailboxes/{mailbox['id']}").json()["status"] == "expired"
    assert client.get(f"/v1/mailboxes/{mailbox['id']}/messages").json() == page
    assert client.get(message_path).json() == message
    assert send_message(client, mailbox, mailbox["email"]).status_code == 410
    service.run_once()
    with service.db.connect() as db:
        row = db.execute("SELECT status, credential_encrypted FROM mailboxes WHERE id=?", (mailbox["id"],)).fetchone()
        assert row["status"] == "expired"
        assert row["credential_encrypted"] is None
        assert db.execute("SELECT COUNT(*) FROM messages WHERE mailbox_id=?", (mailbox["id"],)).fetchone()[0] == 1
    assert client.get(f"/v1/mailboxes/{mailbox['id']}/messages").json() == page
    assert client.get(message_path).json() == message


def test_delete_revokes_access_and_credentials_stay_private(client, providers):
    mailbox = create_mailbox(client)
    service = client.app.state.service
    with service.db.connect() as db:
        row = db.execute("SELECT * FROM mailboxes WHERE id=?", (mailbox["id"],)).fetchone()
        encrypted = row["credential_encrypted"]
    with providers[0]._connection() as db:
        credential = db.execute("SELECT credential FROM fake_mailboxes WHERE upstream_id=?", (row["upstream_id"],)).fetchone()[0]
    assert encrypted != credential
    assert Fernet(service.settings.encryption_key.encode()).decrypt(encrypted.encode()).decode() == credential
    for path in (f"/v1/mailboxes/{mailbox['id']}", "/v1/mailboxes", "/v1/capabilities"):
        response = client.get(path)
        assert credential not in response.text
        assert encrypted not in response.text
    response = client.delete(f"/v1/mailboxes/{mailbox['id']}", headers={"Idempotency-Key": "delete-once"})
    assert response.status_code == 202, response.text
    service.run_once()
    completed = operation(client, response.json()["id"])
    assert completed["status"] == "succeeded"
    assert client.get(f"/v1/mailboxes/{mailbox['id']}").status_code == 410
    assert send_message(client, mailbox, mailbox["email"]).status_code == 410
    with service.db.connect() as db:
        row = db.execute("SELECT status, credential_encrypted FROM mailboxes WHERE id=?", (mailbox["id"],)).fetchone()
        assert row["status"] == "deleted"
        assert row["credential_encrypted"] is None


def test_fake_external_delivery_fails_explicitly(client):
    mailbox = create_mailbox(client)
    response = send_message(client, mailbox, "recipient@example.com")
    assert response.status_code == 202
    client.app.state.service.run_once()
    completed = operation(client, response.json()["id"])
    assert completed["status"] == "failed"
    assert completed["error_code"] == "FAKE_EXTERNAL_SEND_UNSUPPORTED"


class UncertainProvider(FakeProvider):
    send_calls = 0

    def send_message(self, mailbox, recipients, subject, text, request_id):
        self.send_calls += 1
        raise ProviderError("UPSTREAM_TIMEOUT", "Upstream may already have accepted this send", uncertain=True)


def test_uncertain_send_is_not_retried(settings, tmp_path):
    provider = UncertainProvider("uncertain", str(tmp_path / "upstream.sqlite"))
    with app_client(settings, [provider]) as client:
        mailbox = create_mailbox(client, "uncertain")
        response = send_message(client, mailbox, mailbox["email"], key="uncertain-send")
        assert response.status_code == 202
        client.app.state.service.run_once()
        completed = operation(client, response.json()["id"])
        assert completed["status"] == "unknown"
        assert completed["error_code"] == "UPSTREAM_TIMEOUT"
        replay = send_message(client, mailbox, mailbox["email"], key="uncertain-send")
        assert replay.json()["id"] == completed["id"]
        assert replay.json()["status"] == "unknown"
        client.app.state.service.run_once()
        assert provider.send_calls == 1


def test_abandoned_running_operation_becomes_unknown_without_resending(settings, tmp_path):
    provider = UncertainProvider("uncertain", str(tmp_path / "upstream.sqlite"))
    with app_client(settings, [provider]) as client:
        mailbox = create_mailbox(client, "uncertain")
        response = send_message(client, mailbox, mailbox["email"], key="interrupted-send")
        assert response.status_code == 202
        operation_id = response.json()["id"]
        with client.app.state.service.db.connect(write=True) as db:
            db.execute("UPDATE operations SET status='running', updated_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", operation_id))
    with app_client(settings, [provider]) as client:
        client.app.state.service.run_once()
        assert operation(client, operation_id)["status"] == "unknown"
        client.app.state.service.run_once()
        assert provider.send_calls == 0


def test_ttl_requirement_filters_provider_before_creation(settings, tmp_path):
    path = str(tmp_path / "upstream.sqlite")
    short_lived = FakeProvider("short_lived", path)
    short_lived.capabilities = Capabilities(send=True, max_ttl_seconds=600)
    long_lived = FakeProvider("long_lived", path)
    with app_client(settings, [short_lived, long_lived]) as client:
        mailbox = create_mailbox(client, "auto", ttl_seconds=3600)
        assert mailbox["provider_id"] == "long_lived"
        rejected = client.post(
            "/v1/mailboxes", json={"provider": "short_lived", "ttl_seconds": 3600}, headers={"Idempotency-Key": "unsupported-ttl"}
        )
        assert rejected.status_code == 422
        assert error_code(rejected) == "TTL_UNSUPPORTED"


def test_mutations_require_idempotency_and_validate_input(client):
    assert client.post("/v1/mailboxes", json={}).status_code == 422
    invalid = client.post("/v1/mailboxes", json={"ttl_seconds": 0}, headers={"Idempotency-Key": "invalid-ttl"})
    assert invalid.status_code == 422
    unknown_field = client.post("/v1/mailboxes", json={"secret": "unexpected"}, headers={"Idempotency-Key": "unknown-field"})
    assert unknown_field.status_code == 422
    mailbox = create_mailbox(client)
    assert client.delete(f"/v1/mailboxes/{mailbox['id']}").status_code == 422
    response = client.post(f"/v1/mailboxes/{mailbox['id']}/messages", json={"recipients": [mailbox["email"]], "text": "hello"})
    assert response.status_code == 422


def test_upstream_earlier_expiry_takes_precedence(settings, providers, monkeypatch):
    provider = providers[0]
    upstream_expiry = (datetime.now(UTC) + timedelta(seconds=120)).isoformat()
    original_create = provider.create_mailbox

    def create_with_earlier_expiry(ttl_seconds, request_id):
        return replace(original_create(ttl_seconds, request_id), expires_at=upstream_expiry)

    monkeypatch.setattr(provider, "create_mailbox", create_with_earlier_expiry)
    with app_client(settings, providers) as client:
        mailbox = create_mailbox(client, ttl_seconds=3600)
        assert mailbox["expires_at"] == upstream_expiry
        assert mailbox["status"] == "active"


def test_slow_original_worker_can_complete_recovered_unknown_operation(settings, providers, monkeypatch):
    entered, release = Event(), Event()
    provider = providers[0]
    original_create = provider.create_mailbox

    def blocked_create(ttl_seconds, request_id):
        entered.set()
        assert release.wait(timeout=5), "Timed out waiting for the recovery worker"
        return original_create(ttl_seconds, request_id)

    create_spy = Mock(side_effect=blocked_create)
    monkeypatch.setattr(provider, "create_mailbox", create_spy)
    first = Service(settings, Registry(providers))
    second = Service(settings, Registry(providers))
    payload = {"provider": provider.id, "required_capabilities": ["receive"], "ttl_seconds": 3600}
    submitted = first.submit("create", "slow-original-worker", payload)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.run_once)
        try:
            assert entered.wait(timeout=5), "Original worker did not reach the provider"
            with second.db.connect(write=True) as db:
                db.execute(
                    "UPDATE operations SET updated_at=? WHERE id=? AND status='running'",
                    ("2000-01-01T00:00:00+00:00", submitted["id"]),
                )
            second.run_once()
            recovered = second.get_operation(submitted["id"])
            assert recovered["status"] == "unknown"
            assert recovered["error_code"] == "WORKER_INTERRUPTED"
            second.run_once()
            assert create_spy.call_count == 1
        finally:
            release.set()
        assert running.result(timeout=5)["operations"] == 1
    completed = second.get_operation(submitted["id"])
    assert completed["status"] == "succeeded"
    assert completed["error_code"] is None
    mailbox = second.get_mailbox(completed["result"]["mailbox_id"])
    assert mailbox["provider_id"] == provider.id
    assert mailbox["email"] == completed["result"]["email"]
    assert len(second.list_mailboxes(50, 0)["items"]) == 1
    assert create_spy.call_count == 1
