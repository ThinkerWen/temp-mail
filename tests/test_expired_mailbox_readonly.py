from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import Mock
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.providers.registry import Registry
from tests.fakes import ConsumingProvider, create_mailbox, expire, preview


@pytest.fixture
def setup(tmp_path):
    settings = Settings(str(tmp_path / "gateway.db"), "readonly-test-token-with-enough-entropy", Fernet.generate_key().decode())
    provider = ConsumingProvider(str(tmp_path / "upstream.db"))
    with TestClient(create_app(settings, Registry([provider])), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        service = client.app.state.service
        mailbox_id, _ = create_mailbox(service)
        assert service.sync_mailbox(mailbox_id)
        yield client, service, provider, mailbox_id


def cache_snapshot(client, mailbox_id):
    page = client.get(f"/v1/mailboxes/{mailbox_id}/messages")
    assert page.status_code == 200
    message_id = page.json()["items"][0]["id"]
    message = client.get(f"/v1/mailboxes/{mailbox_id}/messages/{message_id}")
    assert message.status_code == 200
    return page.json(), message.json()


def test_expired_mail_remains_readable_before_worker_after_restart_and_until_cleanup(setup):
    client, service, provider, mailbox_id = setup
    before = cache_snapshot(client, mailbox_id)
    expire(service, mailbox_id)
    mailbox = client.get(f"/v1/mailboxes/{mailbox_id}")
    assert mailbox.status_code == 200
    assert mailbox.json()["status"] == "expired"
    assert cache_snapshot(client, mailbox_id) == before
    assert service.expire_mailboxes() == 1
    assert service.expire_mailboxes() == 0
    assert cache_snapshot(client, mailbox_id) == before
    with TestClient(
        create_app(service.settings, Registry([provider])), headers={"Authorization": f"Bearer {service.settings.api_token}"}
    ) as restarted:
        assert restarted.get(f"/v1/mailboxes/{mailbox_id}").json()["status"] == "expired"
        assert cache_snapshot(restarted, mailbox_id) == before
        assert restarted.post("/v1/mailboxes/cleanup", json=preview(restarted)).json() == {"deleted_count": 1}
        for suffix in ("", "/messages", f"/messages/{before[1]['id']}"):
            assert restarted.get(f"/v1/mailboxes/{mailbox_id}{suffix}").status_code == 404
        assert restarted.get("/v1/operations").json()["total"] == 1


@pytest.mark.parametrize("state,expected", [("other-owner", 404), ("deleted", 410)])
def test_readonly_expiration_preserves_owner_isolation_and_deleted_denial(setup, state, expected):
    client, service, _, mailbox_id = setup
    _, message = cache_snapshot(client, mailbox_id)
    expire(service, mailbox_id)
    with service.db.connect(write=True) as conn:
        if state == "other-owner":
            conn.execute("UPDATE mailboxes SET owner_id='another-owner' WHERE id=?", (mailbox_id,))
        else:
            conn.execute("UPDATE mailboxes SET status='deleted' WHERE id=?", (mailbox_id,))
    for suffix in ("", "/messages", f"/messages/{message['id']}"):
        response = client.get(f"/v1/mailboxes/{mailbox_id}{suffix}")
        assert response.status_code == expected
        assert response.json()["error"]["code"] == ("MAILBOX_NOT_FOUND" if state == "other-owner" else "MAILBOX_DELETED")


def test_expiration_retains_unparsed_batches_without_sending_or_receiving(setup, monkeypatch):
    client, service, provider, mailbox_id = setup
    before = cache_snapshot(client, mailbox_id)
    provider.fail_parsing = True
    assert service.sync_mailbox(mailbox_id) is False
    with service.db.connect() as conn:
        batch = dict(conn.execute("SELECT * FROM message_batches WHERE mailbox_id=?", (mailbox_id,)).fetchone())
    operation = service.submit(
        "send", uuid4().hex, {"recipients": ["recipient@example.com"], "subject": "test", "text": "message"}, mailbox_id
    )
    expire(service, mailbox_id)
    methods = ["fetch_messages", "parse_messages", "list_messages", "send_message", "delete_mailbox"]
    requests = {method: Mock(side_effect=AssertionError("Expired mailbox cannot contact or parse upstream mail")) for method in methods}
    for method, mock in requests.items():
        monkeypatch.setattr(provider, method, mock)
    response = client.post(
        f"/v1/mailboxes/{mailbox_id}/messages",
        headers={"Idempotency-Key": uuid4().hex},
        json={"recipients": ["recipient@example.com"], "text": "rejected"},
    )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "MAILBOX_EXPIRED"
    assert service.run_once() == {"operations": 1, "synced": 0, "expired": 1}
    assert service.get_operation(operation["id"])["error_code"] == "MAILBOX_EXPIRED"
    assert service.get_operation(operation["id"])["status"] == "failed"
    assert service.due_mailboxes(10) == []
    assert service.sync_mailbox(mailbox_id) is False
    assert cache_snapshot(client, mailbox_id) == before
    with service.db.connect() as conn:
        assert dict(conn.execute("SELECT * FROM message_batches WHERE mailbox_id=?", (mailbox_id,)).fetchone()) == batch
        mailbox = conn.execute("SELECT status, credential_encrypted FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
        assert mailbox["status"] == "expired"
        assert mailbox["credential_encrypted"] is None
    for mock in requests.values():
        mock.assert_not_called()
    assert client.post("/v1/mailboxes/cleanup", json=preview(client)).json() == {"deleted_count": 1}
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM message_batches").fetchone()[0] == 0


@pytest.mark.parametrize("phase", ["fetch", "parse", "nonconsuming"])
def test_sync_crossing_expiry_preserves_existing_cache(setup, monkeypatch, phase):
    client, service, provider, mailbox_id = setup
    before = cache_snapshot(client, mailbox_id)
    entered, release = Event(), Event()
    if phase == "nonconsuming":
        provider.capabilities = replace(provider.capabilities, destructive_receive=False)
        method = "list_messages"
    else:
        method = "fetch_messages" if phase == "fetch" else "parse_messages"
    original = getattr(provider, method)

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(provider, method, blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(service.sync_mailbox, mailbox_id)
        try:
            assert entered.wait(5)
            expire(service, mailbox_id)
            assert service.expire_mailboxes() == 1
            assert cache_snapshot(client, mailbox_id) == before
        finally:
            release.set()
        assert running.result(timeout=5) is False
    assert cache_snapshot(client, mailbox_id) == before
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_batches WHERE mailbox_id=?", (mailbox_id,)).fetchone()[0] == int(phase == "parse")
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE mailbox_id=?", (mailbox_id,)).fetchone()[0] == 1
