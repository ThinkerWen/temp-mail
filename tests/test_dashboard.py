import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app.service as service_module
from app.api import create_app
from app.config import Settings
from app.providers.registry import Registry

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


class DashboardTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(service_module, "datetime", DashboardTime)
    settings = Settings(str(tmp_path / "dashboard.db"), "dashboard-test-token-with-enough-entropy", Fernet.generate_key().decode())
    with TestClient(create_app(settings, Registry([])), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        yield client, client.app.state.service


def mailbox(
    service, identifier, *, owner="default", provider="provider-a", status="active", created=None, expires=None, error=None, synced=None
):
    with service.db.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO mailboxes
            (id, owner_id, email, provider_id, upstream_id, credential_encrypted, capabilities, status,
            created_at, expires_at, next_sync_at, last_sync_error_code, last_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?)""",
            (
                identifier,
                owner,
                f"{identifier}@example.com",
                provider,
                identifier,
                "private-mailbox-credential",
                status,
                (created or NOW).isoformat(),
                (expires or NOW + timedelta(hours=1)).isoformat(),
                NOW.isoformat(),
                error,
                synced.isoformat() if synced else None,
            ),
        )


def message(service, identifier, mailbox_id, *, cached=None, received=None):
    with service.db.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO messages
            (id, mailbox_id, upstream_id, sender, recipients, subject, text, received_at, cached_at)
            VALUES (?, ?, ?, 'private-sender', '["private-recipient"]', 'private-subject', 'private-message-body', ?, ?)""",
            (identifier, mailbox_id, identifier, (received or NOW).isoformat(), (cached or NOW).isoformat()),
        )


def operation(service, identifier, *, status="succeeded", owner="default", mailbox_id=None, updated=None):
    with service.db.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO operations
            (id, owner_id, kind, idempotency_key, request_hash, payload, provider_id, mailbox_id, status, result, created_at, updated_at)
            VALUES (?, ?, 'create', ?, 'private-request-hash', '{"secret":"private-operation-payload"}', 'provider-a', ?, ?, '{}', ?, ?)""",
            (identifier, owner, f"private-key-{identifier}", mailbox_id, status, NOW.isoformat(), (updated or NOW).isoformat()),
        )


def dashboard(client):
    response = client.get("/v1/dashboard")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_dashboard_empty_data_is_zero_filled(setup):
    client, _ = setup
    data = dashboard(client)
    assert data == {
        "generated_at": NOW.isoformat(),
        "mailboxes": {"total": 0, "active": 0, "expired": 0, "deleted": 0, "sync_errors": 0},
        "messages": {"total": 0, "received_24h": 0},
        "operations": {"total": 0, "pending": 0, "running": 0, "succeeded": 0, "failed": 0, "unknown": 0},
        "last_synced_at": None,
        "activity": [{"date": f"2026-10-{day:02d}", "mailboxes": 0, "messages": 0} for day in range(3, 10)],
        "recent_operations": [],
        "provider_stats": [],
    }


@pytest.mark.parametrize("token", [None, "incorrect-token"])
def test_dashboard_requires_authentication(setup, token):
    client, _ = setup
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client.headers.pop("Authorization")
    response = client.get("/v1/dashboard", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"


def test_dashboard_counts_all_rows_with_owner_expiry_and_deleted_visibility(setup):
    client, service = setup
    latest_sync = NOW - timedelta(minutes=10)
    mailbox(service, "active", error="PROVIDER_TIMEOUT", synced=latest_sync)
    mailbox(service, "unmarked-expired", expires=NOW, error="PROVIDER_TIMEOUT", provider="provider-b")
    mailbox(service, "marked-expired", status="expired", expires=NOW - timedelta(hours=1), error="PROVIDER_TIMEOUT", provider="provider-b")
    mailbox(service, "deleted", status="deleted", synced=NOW)
    mailbox(service, "other", owner="other-owner", provider="other-provider", error="PROVIDER_TIMEOUT", synced=NOW)
    for index in range(7):
        mailbox(service, f"extra-{index}")
    message(service, "visible-active", "active")
    message(service, "visible-expired", "marked-expired")
    message(service, "visible-unmarked", "unmarked-expired", cached=NOW - timedelta(days=2))
    message(service, "hidden-deleted", "deleted")
    message(service, "hidden-owner", "other")
    statuses = ["pending", "running", "succeeded", "failed", "unknown", "succeeded", "succeeded"]
    for index, status in enumerate(statuses):
        operation(service, f"op-{index}", status=status, updated=NOW - timedelta(minutes=index))
    operation(service, "hidden-operation", owner="other-owner")
    # Other list requests and pagination must not influence aggregate data.
    assert client.get("/v1/mailboxes", params={"limit": 5, "email": "missing@example.com"}).json()["total"] == 0
    data = dashboard(client)
    assert data["mailboxes"] == {"total": 11, "active": 8, "expired": 2, "deleted": 1, "sync_errors": 1}
    assert data["messages"] == {"total": 3, "received_24h": 2}
    assert data["operations"] == {"total": 7, "pending": 1, "running": 1, "succeeded": 3, "failed": 1, "unknown": 1}
    assert data["last_synced_at"] == latest_sync.isoformat()
    assert [row["id"] for row in data["recent_operations"]] == [f"op-{index}" for index in range(3)]
    assert data["provider_stats"] == [{"id": "provider-a", "mailboxes": 9, "active": 8}, {"id": "provider-b", "mailboxes": 2, "active": 0}]
    assert sum(day["mailboxes"] for day in data["activity"]) == 11
    assert sum(day["messages"] for day in data["activity"]) == 3


def test_dashboard_activity_uses_seven_utc_days_and_cache_time_boundaries(setup):
    client, service = setup
    start = datetime(2026, 10, 3, tzinfo=UTC)
    timestamps = [start - timedelta(microseconds=1), start, NOW - timedelta(hours=24), NOW, NOW + timedelta(microseconds=1)]
    for index, timestamp in enumerate(timestamps):
        mailbox(service, f"box-{index}", created=timestamp)
        # A very old upstream date must not change when this message first entered the local cache.
        message(service, f"message-{index}", f"box-{index}", cached=timestamp, received=NOW - timedelta(days=365))
    data = dashboard(client)
    expected = [
        {"date": f"2026-10-{day:02d}", "mailboxes": int(day in {3, 8, 9}), "messages": int(day in {3, 8, 9})} for day in range(3, 10)
    ]
    assert data["activity"] == expected
    assert data["messages"] == {"total": 5, "received_24h": 2}
    assert data["generated_at"] == NOW.isoformat()


def test_dashboard_recent_operations_order_by_update_then_id(setup):
    client, service = setup
    for index in range(7):
        operation(service, f"op-{index}")
    operation(service, "old-updated", updated=NOW - timedelta(days=1))
    data = dashboard(client)
    assert data["operations"]["total"] == 8
    assert [row["id"] for row in data["recent_operations"]] == [f"op-{index}" for index in range(6, 3, -1)]
    for row in data["recent_operations"]:
        assert set(row) == {"id", "kind", "status", "provider_id", "mailbox_id", "result", "error_code", "created_at", "updated_at"}


def test_dashboard_is_readonly_and_does_not_read_private_columns_or_contact_providers(setup, monkeypatch):
    client, service = setup
    mailbox(service, "box")
    message(service, "message", "box")
    operation(service, "operation", mailbox_id="box")
    private_columns = {
        "mailboxes": {"email", "upstream_id", "credential_encrypted"},
        "messages": {"sender", "recipients", "subject", "text"},
        "operations": {"payload", "request_hash", "idempotency_key"},
    }
    original_connect = service.db.connect

    def authorize(action, table, column, _database, _trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ and column in private_columns.get(table, set()):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def readonly_connect(*, write=False):
        assert write is False
        with original_connect() as conn:
            conn.set_authorizer(authorize)
            yield conn

    monkeypatch.setattr(service.db, "connect", readonly_connect)

    def reject_provider(*_args, **_kwargs):
        raise AssertionError("Dashboard must not contact a provider")

    monkeypatch.setattr(service.registry, "get", reject_provider)
    monkeypatch.setattr(service.registry, "all", reject_provider)
    data = dashboard(client)
    assert data["mailboxes"]["total"] == 1
    assert "private-" not in str(data)


def test_dashboard_panels_share_one_read_snapshot_during_cleanup(setup, monkeypatch):
    client, service = setup
    mailbox(service, "box")
    message(service, "message", "box")
    original_connect = service.db.connect
    changed = False

    class SnapshotConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, parameters=()):
            nonlocal changed
            result = self.connection.execute(query, parameters)
            if not changed and "MAX(CASE WHEN status!='deleted'" in query:
                changed = True
                with original_connect(write=True) as writer:
                    writer.execute("DELETE FROM messages WHERE mailbox_id='box'")
                    writer.execute("UPDATE mailboxes SET status='deleted' WHERE id='box'")
            return result

    @contextmanager
    def concurrent_connect(*, write=False):
        assert write is False
        with original_connect() as conn:
            yield SnapshotConnection(conn)

    monkeypatch.setattr(service.db, "connect", concurrent_connect)
    data = dashboard(client)
    assert changed
    assert data["mailboxes"]["active"] == 1
    assert data["messages"]["total"] == 1
    assert data["provider_stats"] == [{"id": "provider-a", "mailboxes": 1, "active": 1}]
    monkeypatch.setattr(service.db, "connect", original_connect)
    assert dashboard(client)["messages"]["total"] == 0


def test_dashboard_cleanup_decreases_retained_data_but_preserves_operation_history(setup):
    client, service = setup
    mailbox(service, "expired", expires=NOW - timedelta(days=1))
    message(service, "message", "expired")
    operation(service, "operation", mailbox_id="expired")
    before = dashboard(client)
    assert before["mailboxes"]["expired"] == 1
    assert before["messages"]["total"] == 1
    # Use the same fixed dashboard clock for the confirmed cutoff.
    cutoff = NOW.isoformat()
    revision = service._cleanup_revision(["expired"], cutoff)
    assert service.cleanup_mailboxes(cutoff, 1, revision) == {"deleted_count": 1}
    after = dashboard(client)
    assert after["mailboxes"]["total"] == 0
    assert after["messages"]["total"] == 0
    assert after["operations"]["total"] == 1
    assert after["recent_operations"][0]["mailbox_id"] is None
