from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from threading import Barrier, Event

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.db import Database
from app.providers.registry import Registry
from app.service import Service
from tests.fakes import ConsumingProvider


def create_mailbox(service, key):
    operation = service.submit("create", key, {"provider": "consuming", "ttl_seconds": 3600, "required_capabilities": ["receive"]})
    service._execute_operation(service._claim_operation())
    return service.get_operation(operation["id"])["result"]["mailbox_id"]


@pytest.fixture
def setup(tmp_path):
    settings = Settings(str(tmp_path / "gateway.db"), "a-test-token-with-enough-entropy", Fernet.generate_key().decode())
    provider = ConsumingProvider(str(tmp_path / "upstream.db"))
    service = Service(settings, Registry([provider]))
    return service, provider


def test_consuming_mailboxes_fetch_in_parallel_without_blocking_create(setup, monkeypatch):
    service, provider = setup
    mailboxes = [create_mailbox(service, f"create-{i}") for i in range(2)]
    entered, release = Barrier(3), Event()
    original_fetch = provider.fetch_messages

    def fetch(mailbox):
        entered.wait(timeout=5)
        assert release.wait(5)
        return original_fetch(mailbox)

    monkeypatch.setattr(provider, "fetch_messages", fetch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(service.sync_mailbox, mailbox_id) for mailbox_id in mailboxes]
        try:
            entered.wait(timeout=5)
            created = pool.submit(create_mailbox, service, "create-during-receive")
            assert service.get_mailbox(created.result(timeout=3))["status"] == "active"
        finally:
            release.set()
        assert all(future.result(timeout=5) for future in futures)
    assert provider.fetches == 2


def test_same_mailbox_is_skipped_while_another_service_syncs(setup, monkeypatch):
    service, provider = setup
    mailbox_id = create_mailbox(service, "first")
    other = Service(service.settings, Registry([provider]))
    entered, release = Event(), Event()
    original_fetch = provider.fetch_messages

    def fetch(mailbox):
        entered.set()
        assert release.wait(5)
        return original_fetch(mailbox)

    monkeypatch.setattr(provider, "fetch_messages", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.sync_mailbox, mailbox_id, scheduled=True)
        try:
            assert entered.wait(5)
            second = pool.submit(other.sync_mailbox, mailbox_id, scheduled=True)
            assert second.result(timeout=2) is False
        finally:
            release.set()
        assert first.result(timeout=5) is True
    # A stale due-list entry must not fetch again after the first worker releases its lock.
    assert other.sync_mailbox(mailbox_id, scheduled=True) is False
    assert provider.fetches == 1
    assert len(service.list_messages(mailbox_id, 50, 0)["items"]) == 1


def test_consumed_batch_parsing_does_not_hold_database_write_lock(setup, monkeypatch):
    service, provider = setup
    mailbox_id = create_mailbox(service, "first")
    original_parse = provider.parse_messages
    submitted = []

    def parse(mailbox, payload, batch_id):
        submitted.append(
            service.submit("create", "during-parse", {"provider": "consuming", "ttl_seconds": 3600, "required_capabilities": ["receive"]})
        )
        return original_parse(mailbox, payload, batch_id)

    monkeypatch.setattr(provider, "parse_messages", parse)
    assert service.sync_mailbox(mailbox_id) is True
    assert service.get_operation(submitted[0]["id"])["status"] == "pending"


def test_due_mailboxes_excludes_inflight_expired_and_future_rows(setup):
    service, _ = setup
    mailboxes = [create_mailbox(service, f"create-{i}") for i in range(5)]
    with service.db.connect(write=True) as conn:
        conn.execute("UPDATE mailboxes SET next_sync_at='2000-01-01T00:00:00+00:00'")
        conn.execute("UPDATE mailboxes SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (mailboxes[0],))
        conn.execute("UPDATE mailboxes SET status='deleted' WHERE id=?", (mailboxes[1],))
        conn.execute("UPDATE mailboxes SET next_sync_at='2999-01-01T00:00:00+00:00' WHERE id=?", (mailboxes[2],))
    assert service.due_mailboxes(10) == sorted(mailboxes[3:])
    assert service.due_mailboxes(1) == sorted(mailboxes[3:])[:1]
    assert service.due_mailboxes(10, exclude={mailboxes[3]}) == [mailboxes[4]]
    assert service.due_mailboxes(0) == []


def test_claim_preserves_inflight_operation_but_recovers_abandoned_one(setup):
    service, _ = setup
    operations = [
        service.submit("create", f"operation-{i}", {"provider": "consuming", "ttl_seconds": 3600, "required_capabilities": ["receive"]})
        for i in range(3)
    ]
    running, abandoned = service._claim_operation(), service._claim_operation()
    with service.db.connect(write=True) as conn:
        conn.execute("UPDATE operations SET updated_at='2000-01-01T00:00:00+00:00' WHERE status='running'")
    claimed = service._claim_operation(exclude={running["id"]})
    assert claimed["id"] == operations[2]["id"]
    assert service.get_operation(running["id"])["status"] == "running"
    assert service.get_operation(abandoned["id"])["status"] == "unknown"
    assert service.get_operation(abandoned["id"])["error_code"] == "WORKER_INTERRUPTED"
    # The protected task can still commit its definitive response.
    service._execute_operation(running)
    assert service.get_operation(running["id"])["status"] == "succeeded"


def hold_mailbox_lock(path, entered, release):
    with Database(path).mailbox_lock("shared-mailbox") as acquired:
        if acquired:
            entered.set()
            release.wait(30)


def test_mailbox_lock_excludes_other_process_and_releases_after_termination(tmp_path):
    db = Database(str(tmp_path / "gateway.db"))
    context = get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=hold_mailbox_lock, args=(db.path, entered, release))
    process.start()
    try:
        assert entered.wait(10)
        with db.mailbox_lock("shared-mailbox") as acquired:
            assert acquired is False
        with db.mailbox_lock("other-mailbox") as acquired:
            assert acquired is True
    finally:
        process.terminate()
        process.join(timeout=5)
    assert not process.is_alive()
    with db.mailbox_lock("shared-mailbox") as acquired:
        assert acquired is True


def test_mailbox_lock_releases_on_exception_without_removing_inode(tmp_path):
    db = Database(str(tmp_path / "gateway.db"))
    with pytest.raises(RuntimeError, match="failure"):
        with db.mailbox_lock("shared-mailbox") as acquired:
            assert acquired is True
            path = next((tmp_path / "gateway.db.mailbox-locks").iterdir())
            inode = path.stat().st_ino
            raise RuntimeError("failure")
    assert path.stat().st_ino == inode
    other = Database(db.path)
    with other.mailbox_lock("shared-mailbox") as acquired:
        assert acquired is True
