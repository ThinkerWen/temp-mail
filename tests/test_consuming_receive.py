import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.providers.base import ProviderError, ProviderMessage
from app.providers.registry import Registry
from app.service import Service
from tests.fakes import FakeProvider


class ConsumingProvider(FakeProvider):
    def __init__(self, path):
        super().__init__("consuming", path)
        self.capabilities = replace(self.capabilities, destructive_receive=True)
        self.fetches = 0
        self.fail_parsing = False
        self.batch_ids = []

    def fetch_messages(self, mailbox):
        self.fetches += 1
        return {"body": "private consumed message"}

    def parse_messages(self, mailbox, payload, batch_id):
        self.batch_ids.append(batch_id)
        if self.fail_parsing:
            raise ProviderError("PROVIDER_INVALID_RESPONSE", "Cannot normalize response")
        return [
            ProviderMessage(batch_id + ":0", "sender@example.com", [mailbox.email], "subject", payload["body"], "2026-01-01T00:00:00+00:00")
        ]


@pytest.fixture
def setup(tmp_path):
    settings = Settings(str(tmp_path / "gateway.db"), "a-test-token-with-enough-entropy", Fernet.generate_key().decode())
    provider = ConsumingProvider(str(tmp_path / "upstream.db"))
    service = Service(settings, Registry([provider]))
    operation = service.submit("create", "create-key", {"provider": provider.id, "ttl_seconds": 3600, "required_capabilities": ["receive"]})
    service._execute_operation(service._claim_operation())
    mailbox_id = service.get_operation(operation["id"])["result"]["mailbox_id"]
    return service, provider, mailbox_id


def test_consumed_batch_survives_parser_failure_and_process_restart(setup):
    service, provider, mailbox_id = setup
    provider.fail_parsing = True
    assert service.sync_mailbox(mailbox_id) is False
    with service.db.connect() as conn:
        batch = dict(conn.execute("SELECT * FROM message_batches").fetchone())
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert "private consumed message" not in batch["payload_encrypted"]
    assert json.loads(service.cipher.decrypt(batch["payload_encrypted"].encode())) == {"body": "private consumed message"}
    assert service.sync_mailbox(mailbox_id) is False
    assert provider.fetches == 1
    provider.fail_parsing = False
    restarted = Service(service.settings, Registry([provider]))
    assert restarted.sync_mailbox(mailbox_id) is True
    assert provider.fetches == 1
    assert provider.batch_ids == [batch["id"]] * 3
    with restarted.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM message_batches").fetchone()[0] == 0
        assert conn.execute("SELECT text FROM messages").fetchone()[0] == "private consumed message"


def test_consumed_batch_replays_after_cache_commit_failure(setup, monkeypatch):
    service, provider, mailbox_id = setup
    original_connect = service.db.connect
    writes = 0

    @contextmanager
    def fail_second_write(*, write=False):
        nonlocal writes
        with original_connect(write=write) as conn:
            if write:
                writes += 1
            yield conn
            if write and writes == 2:
                raise sqlite3.OperationalError("simulated cache commit failure")

    monkeypatch.setattr(service.db, "connect", fail_second_write)
    assert service.sync_mailbox(mailbox_id) is False
    with original_connect() as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM message_batches").fetchone()[0] == 1
    monkeypatch.setattr(service.db, "connect", original_connect)
    assert service.sync_mailbox(mailbox_id) is True
    assert provider.fetches == 1
    assert len(service.list_messages(mailbox_id, 50, 0)["items"]) == 1
    assert len(set(provider.batch_ids)) == 1


@pytest.mark.parametrize("action", ["expire", "delete"])
def test_pending_batch_is_removed_with_mailbox_data(setup, action):
    service, provider, mailbox_id = setup
    provider.fail_parsing = True
    assert service.sync_mailbox(mailbox_id) is False
    if action == "expire":
        with service.db.connect(write=True) as conn:
            conn.execute("UPDATE mailboxes SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (mailbox_id,))
        assert service.expire_mailboxes() == 1
    else:
        operation = service.submit("delete", "delete-key", {}, mailbox_id)
        service._execute_operation(service._claim_operation())
        assert service.get_operation(operation["id"])["status"] == "succeeded"
    with service.db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM message_batches").fetchone()[0] == 0
        assert conn.execute("SELECT credential_encrypted FROM mailboxes").fetchone()[0] is None


def test_concurrent_workers_reuse_pending_consumed_response(setup, monkeypatch):
    service, provider, mailbox_id = setup
    entered, release = Event(), Event()
    original_fetch = provider.fetch_messages
    provider.fail_parsing = True

    def blocking_fetch(mailbox):
        entered.set()
        assert release.wait(5)
        return original_fetch(mailbox)

    monkeypatch.setattr(provider, "fetch_messages", blocking_fetch)
    other = Service(service.settings, Registry([provider]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.sync_mailbox, mailbox_id)
        assert entered.wait(5)
        second = pool.submit(other.sync_mailbox, mailbox_id)
        release.set()
        assert first.result(timeout=5) is False
        assert second.result(timeout=5) is False
    assert provider.fetches == 1
    assert len(set(provider.batch_ids)) == 1
