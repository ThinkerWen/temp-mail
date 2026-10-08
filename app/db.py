import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS mailboxes (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    email TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    upstream_id TEXT NOT NULL,
    credential_encrypted TEXT,
    capabilities TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'expired', 'deleted')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_synced_at TEXT,
    next_sync_at TEXT NOT NULL,
    sync_failures INTEGER NOT NULL DEFAULT 0,
    last_sync_error_code TEXT,
    UNIQUE(provider_id, upstream_id)
);
CREATE INDEX IF NOT EXISTS mailboxes_owner_email ON mailboxes(owner_id, email);
CREATE INDEX IF NOT EXISTS mailboxes_sync ON mailboxes(status, next_sync_at);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('create', 'send', 'delete')),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    mailbox_id TEXT REFERENCES mailboxes(id),
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'succeeded', 'failed', 'unknown')),
    result TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id, kind, idempotency_key)
);
CREATE INDEX IF NOT EXISTS operations_queue ON operations(status, created_at);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    mailbox_id TEXT NOT NULL REFERENCES mailboxes(id),
    upstream_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    text TEXT NOT NULL,
    received_at TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    UNIQUE(mailbox_id, upstream_id)
);
CREATE INDEX IF NOT EXISTS messages_mailbox ON messages(mailbox_id, received_at);
CREATE TABLE IF NOT EXISTS message_batches (
    mailbox_id TEXT PRIMARY KEY REFERENCES mailboxes(id),
    id TEXT NOT NULL UNIQUE,
    payload_encrypted TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = str(Path(path).expanduser().resolve())

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self, *, write: bool = False):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
