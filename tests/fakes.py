"""Persistent upstream fake used only by offline tests."""

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.providers.base import Capabilities, ProviderError, ProviderMailbox, ProviderMessage

SCHEMA = """
CREATE TABLE IF NOT EXISTS fake_mailboxes (
    upstream_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    credential TEXT NOT NULL,
    expires_at REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fake_requests (
    provider_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (provider_id, kind, request_id)
);
CREATE TABLE IF NOT EXISTS fake_messages (
    upstream_id TEXT NOT NULL,
    mailbox_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipients TEXT NOT NULL,
    subject TEXT NOT NULL,
    text TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (upstream_id, mailbox_id)
);
"""


class FakeProvider:
    def __init__(self, provider_id: str, db_path: str, can_send: bool = True):
        if not re.fullmatch(r"[a-z0-9_]+", provider_id):
            raise ValueError("Fake provider IDs use lowercase letters, numbers, and underscores")
        if str(db_path) == ":memory:":
            raise ValueError("FakeProvider requires a persistent SQLite file")
        self.id = provider_id
        self.capabilities = Capabilities(send=can_send)
        self.db_path = str(Path(db_path).expanduser().resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        except sqlite3.OperationalError:
            raise ProviderError("PROVIDER_UNAVAILABLE", "Fake provider storage is unavailable") from None
        finally:
            db.close()

    def _replay(self, db: sqlite3.Connection, kind: str, request_id: str, payload: list) -> tuple[str, str | None]:
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        row = db.execute(
            "SELECT payload_hash, result FROM fake_requests WHERE provider_id=? AND kind=? AND request_id=?",
            (self.id, kind, request_id),
        ).fetchone()
        if row and row["payload_hash"] != digest:
            raise ProviderError("IDEMPOTENCY_CONFLICT", "Request ID was already used with different arguments")
        return digest, row["result"] if row else None

    def _remember(self, db: sqlite3.Connection, kind: str, request_id: str, digest: str, result: str) -> None:
        db.execute("INSERT INTO fake_requests VALUES (?, ?, ?, ?, ?)", (self.id, kind, request_id, digest, result))

    def _authenticate(self, db: sqlite3.Connection, mailbox: ProviderMailbox, require_active: bool = True) -> sqlite3.Row:
        row = db.execute("SELECT * FROM fake_mailboxes WHERE upstream_id=? AND provider_id=?", (mailbox.upstream_id, self.id)).fetchone()
        if not row:
            raise ProviderError("MAILBOX_NOT_FOUND", "Mailbox does not exist at the bound provider")
        if mailbox.email != row["email"] or not secrets.compare_digest(mailbox.credential, row["credential"]):
            raise ProviderError("INVALID_CREDENTIAL", "Invalid upstream mailbox credentials")
        if require_active and row["deleted"]:
            raise ProviderError("MAILBOX_NOT_FOUND", "Mailbox has been deleted")
        if require_active and row["expires_at"] <= datetime.now(UTC).timestamp():
            raise ProviderError("MAILBOX_EXPIRED", "Mailbox has expired")
        return row

    def create_mailbox(self, ttl_seconds: int, request_id: str) -> ProviderMailbox:
        if not 0 < ttl_seconds <= self.capabilities.max_ttl_seconds:
            raise ProviderError("TTL_UNSUPPORTED", "Requested mailbox lifetime is unsupported")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            digest, previous = self._replay(db, "create", request_id, [ttl_seconds])
            if previous:
                return ProviderMailbox(**json.loads(previous))
            upstream_id = uuid4().hex
            expires_at = datetime.now(UTC).timestamp() + ttl_seconds
            mailbox = ProviderMailbox(
                upstream_id,
                f"{upstream_id}@{self.id.replace('_', '-')}.test",
                secrets.token_urlsafe(32),
                datetime.fromtimestamp(expires_at, UTC).isoformat(),
            )
            db.execute(
                "INSERT INTO fake_mailboxes (upstream_id, provider_id, email, credential, expires_at) VALUES (?, ?, ?, ?, ?)",
                (upstream_id, self.id, mailbox.email, mailbox.credential, expires_at),
            )
            result = json.dumps(
                {"upstream_id": upstream_id, "email": mailbox.email, "credential": mailbox.credential, "expires_at": mailbox.expires_at}
            )
            self._remember(db, "create", request_id, digest, result)
            return mailbox

    def list_messages(self, mailbox: ProviderMailbox) -> list[ProviderMessage]:
        with self._connection() as db:
            self._authenticate(db, mailbox)
            rows = db.execute(
                "SELECT * FROM fake_messages WHERE mailbox_id=? ORDER BY received_at, upstream_id", (mailbox.upstream_id,)
            ).fetchall()
            return [
                ProviderMessage(
                    row["upstream_id"], row["sender"], json.loads(row["recipients"]), row["subject"], row["text"], row["received_at"]
                )
                for row in rows
            ]

    def send_message(self, mailbox: ProviderMailbox, recipients: list[str], subject: str, text: str, request_id: str) -> str:
        if not self.capabilities.send:
            raise ProviderError("CAPABILITY_UNSUPPORTED", "This provider does not support sending")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authenticate(db, mailbox, require_active=False)
            digest, previous = self._replay(db, "send", request_id, [mailbox.upstream_id, recipients, subject, text])
            if previous:
                return previous
            self._authenticate(db, mailbox)
            if not recipients:
                raise ProviderError("RECIPIENT_UNAVAILABLE", "At least one recipient is required")
            targets = []
            for address in dict.fromkeys(recipients):
                if not address.lower().endswith(".test"):
                    raise ProviderError("FAKE_EXTERNAL_SEND_UNSUPPORTED", "Fake providers can only send to existing local fake mailboxes")
                target = db.execute("SELECT * FROM fake_mailboxes WHERE email=?", (address,)).fetchone()
                if not target or target["deleted"] or target["expires_at"] <= datetime.now(UTC).timestamp():
                    raise ProviderError("RECIPIENT_UNAVAILABLE", "A fake recipient does not exist or is no longer active")
                targets.append(target["upstream_id"])
            send_id, received_at = uuid4().hex, datetime.now(UTC).isoformat()
            db.executemany(
                "INSERT INTO fake_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(send_id, target, mailbox.email, json.dumps(recipients), subject, text, received_at) for target in targets],
            )
            self._remember(db, "send", request_id, digest, send_id)
            return send_id

    def delete_mailbox(self, mailbox: ProviderMailbox, request_id: str) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authenticate(db, mailbox, require_active=False)
            digest, previous = self._replay(db, "delete", request_id, [mailbox.upstream_id])
            if previous:
                return
            db.execute("UPDATE fake_mailboxes SET deleted=1 WHERE upstream_id=?", (mailbox.upstream_id,))
            db.execute("DELETE FROM fake_messages WHERE mailbox_id=?", (mailbox.upstream_id,))
            self._remember(db, "delete", request_id, digest, "deleted")
