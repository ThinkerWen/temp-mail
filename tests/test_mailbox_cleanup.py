import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
from app.providers.base import ProviderMessage
from app.providers.registry import Registry
from tests.fakes import ConsumingProvider, create_mailbox, expire, preview


@pytest.fixture
def setup(tmp_path):
    settings = Settings(str(tmp_path / "gateway.db"), "cleanup-test-token-with-enough-entropy", Fernet.generate_key().decode())
    provider = ConsumingProvider(str(tmp_path / "upstream.db"))
    with TestClient(create_app(settings, Registry([provider])), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        yield client, client.app.state.service, provider


def test_cleanup_empty_preview_and_noop(setup):
    client, _, _ = setup
    body = preview(client)
    assert body["expected_count"] == 0
    assert len(body["revision"]) == 64
    response = client.post("/v1/mailboxes/cleanup", json=body)
    assert response.status_code == 200
    assert response.json() == {"deleted_count": 0}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("method,path", [("GET", "/v1/mailboxes/cleanup-preview"), ("POST", "/v1/mailboxes/cleanup")])
def test_cleanup_requires_authentication(setup, method, path):
    client, _, _ = setup
    response = client.request(method, path, headers={"Authorization": "Bearer incorrect"})
    assert response.status_code == 401


def test_cleanup_removes_local_cache_and_credentials_but_preserves_history(setup, monkeypatch):
    client, service, provider = setup
    mailbox_id, operation = create_mailbox(service)
    assert service.sync_mailbox(mailbox_id)
    provider.fail_parsing = True
    assert not service.sync_mailbox(mailbox_id)
    expire(service, mailbox_id)
    # Cleanup is entirely local, even when a provider cannot support upstream deletion.
    delete = Mock(side_effect=AssertionError("Cleanup must not contact the provider"))
    monkeypatch.setattr(provider, "delete_mailbox", delete)
    response = client.post("/v1/mailboxes/cleanup", json=preview(client))
    assert response.status_code == 200
    assert response.json() == {"deleted_count": 1}
    delete.assert_not_called()
    with service.db.connect() as conn:
        for table in ("mailboxes", "messages", "message_batches"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert service.get_operation(operation["id"]) == {**operation, "mailbox_id": None}
    assert client.get("/v1/operations").json()["total"] == 1
    assert client.get(f"/v1/mailboxes/{mailbox_id}").status_code == 404


def test_cleanup_selection_is_owner_scoped_and_preserves_live_mailboxes(setup):
    client, service, _ = setup
    ids = [create_mailbox(service)[0] for _ in range(6)]
    with service.db.connect(write=True) as conn:
        conn.execute("UPDATE mailboxes SET expires_at='2000-01-01T00:00:00+00:00' WHERE id IN (?, ?)", (ids[0], ids[1]))
        conn.execute("UPDATE mailboxes SET owner_id='other-owner' WHERE id=?", (ids[1],))
        conn.execute("UPDATE mailboxes SET status='deleted' WHERE id=?", (ids[2],))
        conn.execute("UPDATE mailboxes SET status='expired' WHERE id=?", (ids[3],))
        conn.execute("UPDATE mailboxes SET status='deleted', created_at='2999-01-01T00:00:00+00:00' WHERE id=?", (ids[4],))
    body = preview(client)
    assert body["expected_count"] == 2
    assert client.post("/v1/mailboxes/cleanup", json=body).json() == {"deleted_count": 2}
    with service.db.connect() as conn:
        remaining = {row["id"] for row in conn.execute("SELECT id FROM mailboxes")}
    assert remaining == {ids[1], ids[3], ids[4], ids[5]}


def test_cleanup_cutoff_excludes_mailboxes_expiring_after_confirmation(setup):
    client, service, _ = setup
    selected, _ = create_mailbox(service)
    later, _ = create_mailbox(service)
    expire(service, selected)
    body = preview(client)
    later_expiry = (datetime.fromisoformat(body["cutoff"]) + timedelta(microseconds=1)).isoformat()
    with service.db.connect(write=True) as conn:
        conn.execute("UPDATE mailboxes SET expires_at=?, status='expired' WHERE id=?", (later_expiry, later))
    assert client.post("/v1/mailboxes/cleanup", json=body).json() == {"deleted_count": 1}
    with service.db.connect() as conn:
        assert [row["id"] for row in conn.execute("SELECT id FROM mailboxes")] == [later]


@pytest.mark.parametrize("change", ["count", "same_count_different_ids", "client_count", "revision"])
def test_cleanup_rejects_changed_confirmation_without_deleting(setup, change):
    client, service, _ = setup
    first, _ = create_mailbox(service)
    second, _ = create_mailbox(service)
    expire(service, first)
    body = preview(client)
    if change in {"count", "same_count_different_ids"}:
        expire(service, second)
    if change == "same_count_different_ids":
        with service.db.connect(write=True) as conn:
            conn.execute("UPDATE mailboxes SET expires_at='2999-01-01T00:00:00+00:00' WHERE id=?", (first,))
    elif change == "client_count":
        body["expected_count"] += 1
    elif change == "revision":
        body["revision"] = "0" * 64
    response = client.post("/v1/mailboxes/cleanup", json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CLEANUP_CHANGED"
    assert service.list_mailboxes(50, 0)["total"] == 2


@pytest.mark.parametrize("cutoff", ["invalid", "2026-01-01T00:00:00", "2999-01-01T00:00:00+00:00"])
def test_cleanup_rejects_invalid_or_future_cutoff(setup, cutoff):
    client, _, _ = setup
    body = {**preview(client), "cutoff": cutoff}
    response = client.post("/v1/mailboxes/cleanup", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CLEANUP_INVALID_CUTOFF"


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_count": -1},
        {"expected_count": True},
        {"expected_count": 1.0},
        {"expected_count": "1"},
        {"revision": "invalid"},
        {"unexpected": True},
    ],
)
def test_cleanup_validates_confirmation_body(setup, changes):
    client, _, _ = setup
    response = client.post("/v1/mailboxes/cleanup", json={**preview(client), **changes})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_cleanup_transaction_rolls_back_all_changes_on_storage_failure(setup, monkeypatch):
    client, service, provider = setup
    mailbox_id, operation = create_mailbox(service)
    assert service.sync_mailbox(mailbox_id)
    provider.fail_parsing = True
    assert not service.sync_mailbox(mailbox_id)
    expire(service, mailbox_id)
    body = preview(client)
    original_connect = service.db.connect

    @contextmanager
    def fail_commit(*, write=False):
        with original_connect(write=write) as conn:
            yield conn
            if write:
                raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(service.db, "connect", fail_commit)
    response = client.post("/v1/mailboxes/cleanup", json=body)
    assert response.status_code == 503
    with original_connect() as conn:
        for table in ("mailboxes", "messages", "message_batches"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    assert service.get_operation(operation["id"])["mailbox_id"] == mailbox_id


@pytest.mark.parametrize("destructive", [False, True])
def test_inflight_receive_cannot_restore_cleaned_mailbox_or_cache(setup, monkeypatch, destructive):
    client, service, provider = setup
    mailbox_id, _ = create_mailbox(service)
    provider.capabilities = replace(provider.capabilities, destructive_receive=destructive)
    entered, release = Event(), Event()

    def fetch(mailbox):
        entered.set()
        assert release.wait(5)
        if destructive:
            return {"body": "mail received during cleanup"}
        return [ProviderMessage("late", "sender@example.com", [mailbox.email], "late mail", "text", datetime.now(UTC).isoformat())]

    monkeypatch.setattr(provider, "fetch_messages" if destructive else "list_messages", fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(service.sync_mailbox, mailbox_id)
        try:
            assert entered.wait(5)
            expire(service, mailbox_id)
            assert client.post("/v1/mailboxes/cleanup", json=preview(client)).json() == {"deleted_count": 1}
        finally:
            release.set()
        assert running.result(timeout=5) is False
    with service.db.connect() as conn:
        for table in ("mailboxes", "messages", "message_batches"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["send", "delete"])
def test_inflight_operation_finishes_with_history_after_mailbox_cleanup(setup, monkeypatch, kind):
    client, service, provider = setup
    mailbox_id, _ = create_mailbox(service)
    payload = {"recipients": ["recipient@example.com"], "subject": "test", "text": "message"} if kind == "send" else {}
    operation = service.submit(kind, uuid4().hex, payload, mailbox_id)
    claimed = service._claim_operation()
    entered, release = Event(), Event()

    def request(*_args):
        entered.set()
        assert release.wait(5)
        return "sent-message" if kind == "send" else None

    monkeypatch.setattr(provider, "send_message" if kind == "send" else "delete_mailbox", request)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(service._execute_operation, claimed)
        try:
            assert entered.wait(5)
            expire(service, mailbox_id)
            assert client.post("/v1/mailboxes/cleanup", json=preview(client)).json() == {"deleted_count": 1}
        finally:
            release.set()
        running.result(timeout=5)
    saved = service.get_operation(operation["id"])
    assert saved["status"] == "succeeded"
    assert saved["mailbox_id"] is None
    assert saved["result"] == (
        {"provider_message_id": "sent-message", "delivery_status": "accepted"}
        if kind == "send"
        else {"mailbox_id": mailbox_id, "status": "deleted"}
    )
    assert service.list_mailboxes(50, 0)["total"] == 0


def test_pending_operation_on_cleaned_mailbox_fails_without_calling_provider(setup, monkeypatch):
    client, service, provider = setup
    mailbox_id, _ = create_mailbox(service)
    operation = service.submit("delete", uuid4().hex, {}, mailbox_id)
    expire(service, mailbox_id)
    assert client.post("/v1/mailboxes/cleanup", json=preview(client)).json() == {"deleted_count": 1}
    delete = Mock(side_effect=AssertionError("Deleted local mailbox must not be contacted"))
    monkeypatch.setattr(provider, "delete_mailbox", delete)
    service._execute_operation(service._claim_operation())
    saved = service.get_operation(operation["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "MAILBOX_NOT_FOUND"
    assert saved["mailbox_id"] is None
    delete.assert_not_called()
