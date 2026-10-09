import hashlib
import json
from collections.abc import Callable, Collection
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from app.config import Settings
from app.db import Database, utcnow
from app.errors import ServiceError
from app.providers import ProviderError, ProviderMailbox, Registry
from app.providers.base import DestructiveReceiveProvider
from app.providers.factory import build_registry

OWNER_ID = "default"


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def provider_error(exc: ProviderError) -> ServiceError:
    status = 503 if exc.code == "PROVIDER_UNAVAILABLE" else 422
    return ServiceError(exc.code, "The provider cannot satisfy this request", status)


class Service:
    def __init__(self, settings: Settings, registry: Registry | None = None, *, db: Database | None = None, cipher: Fernet | None = None):
        self.settings = settings
        self.db = db if db is not None else Database(settings.db_path)
        if db is None:
            self.db.initialize()
        self.cipher = cipher if cipher is not None else Fernet(settings.encryption_key.encode())
        self.registry = registry if registry is not None else build_registry(settings)

    @staticmethod
    def operation_view(row) -> dict:
        fields = ("id", "kind", "status", "provider_id", "mailbox_id", "error_code", "created_at", "updated_at")
        return {**{field: row[field] for field in fields}, "result": json.loads(row["result"]) if row["result"] else None}

    @staticmethod
    def mailbox_view(row) -> dict:
        fields = ("id", "email", "provider_id", "status", "created_at", "expires_at", "last_synced_at", "last_sync_error_code")
        result = {field: row[field] for field in fields}
        result["capabilities"] = json.loads(row["capabilities"])
        if result["status"] == "active" and result["expires_at"] <= utcnow():
            result["status"] = "expired"
        return result

    def _mailbox(self, conn, mailbox_id: str, *, active: bool = True, allow_expired: bool = False):
        row = conn.execute("SELECT * FROM mailboxes WHERE id=? AND owner_id=?", (mailbox_id, OWNER_ID)).fetchone()
        if row is None:
            raise ServiceError("MAILBOX_NOT_FOUND", "Mailbox not found", 404)
        if active and row["status"] == "deleted":
            raise ServiceError("MAILBOX_DELETED", "Mailbox has been deleted", 410)
        if active and not allow_expired and (row["status"] == "expired" or row["expires_at"] <= utcnow()):
            raise ServiceError("MAILBOX_EXPIRED", "Mailbox has expired", 410)
        return row

    def get_mailbox(self, mailbox_id: str) -> dict:
        with self.db.connect() as conn:
            return self.mailbox_view(self._mailbox(conn, mailbox_id, allow_expired=True))

    def list_mailboxes(self, limit: int, offset: int, email: str | None = None) -> dict:
        where, args = "WHERE owner_id=?", [OWNER_ID]
        if email is not None:
            where += " AND email=?"
            args.append(email)
        with self.db.connect() as conn:
            # Count and page share one snapshot while concurrent workers create or expire mailboxes.
            conn.execute("BEGIN")
            total = conn.execute(f"SELECT COUNT(*) FROM mailboxes {where}", args).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM mailboxes {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", [*args, limit, offset]
            ).fetchall()
        return {"items": [self.mailbox_view(row) for row in rows], "limit": limit, "offset": offset, "total": total}

    @staticmethod
    def _cleanup_candidates(conn, cutoff: str) -> list[str]:
        rows = conn.execute(
            "SELECT id FROM mailboxes WHERE owner_id=? AND created_at<=? AND (status='deleted' OR expires_at<=?) ORDER BY id",
            (OWNER_ID, cutoff, cutoff),
        ).fetchall()
        return [row["id"] for row in rows]

    @staticmethod
    def _cleanup_revision(mailbox_ids: list[str], cutoff: str) -> str:
        return hashlib.sha256(encode({"owner_id": OWNER_ID, "cutoff": cutoff, "mailbox_ids": mailbox_ids}).encode()).hexdigest()

    def mailbox_cleanup_preview(self) -> dict:
        cutoff = utcnow()
        with self.db.connect() as conn:
            mailbox_ids = self._cleanup_candidates(conn, cutoff)
        return {"count": len(mailbox_ids), "cutoff": cutoff, "revision": self._cleanup_revision(mailbox_ids, cutoff)}

    def cleanup_mailboxes(self, cutoff: str, expected_count: int, revision: str) -> dict:
        try:
            parsed = datetime.fromisoformat(cutoff)
            if parsed.tzinfo is None or parsed > datetime.now(UTC):
                raise ValueError("Invalid cleanup cutoff")
            cutoff = parsed.astimezone(UTC).isoformat()
        except (ValueError, OverflowError):
            raise ServiceError("CLEANUP_INVALID_CUTOFF", "Cleanup cutoff must be a past timestamp with a timezone", 422) from None
        with self.db.connect(write=True) as conn:
            mailbox_ids = self._cleanup_candidates(conn, cutoff)
            if len(mailbox_ids) != expected_count or self._cleanup_revision(mailbox_ids, cutoff) != revision:
                raise ServiceError("CLEANUP_CHANGED", "The mailboxes eligible for cleanup changed; please confirm again", 409)
            arguments = [(mailbox_id,) for mailbox_id in mailbox_ids]
            conn.executemany("DELETE FROM messages WHERE mailbox_id=?", arguments)
            conn.executemany("DELETE FROM message_batches WHERE mailbox_id=?", arguments)
            # Keep operation history and its original result, while releasing the foreign key to deleted metadata.
            conn.executemany("UPDATE operations SET mailbox_id=NULL WHERE mailbox_id=?", arguments)
            conn.executemany("DELETE FROM mailboxes WHERE id=?", arguments)
        return {"deleted_count": len(mailbox_ids)}

    def list_operations(self, limit: int, offset: int) -> dict:
        with self.db.connect() as conn:
            # Keep the count and page consistent while the worker or another request updates operations.
            conn.execute("BEGIN")
            total = conn.execute("SELECT COUNT(*) FROM operations WHERE owner_id=?", (OWNER_ID,)).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM operations WHERE owner_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (OWNER_ID, limit, offset),
            ).fetchall()
        return {"items": [self.operation_view(row) for row in rows], "limit": limit, "offset": offset, "total": total}

    def dashboard(self) -> dict:
        now = datetime.now(UTC)
        generated_at = now.isoformat()
        day_start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        activity = {(day_start + timedelta(days=index)).date().isoformat(): {"mailboxes": 0, "messages": 0} for index in range(7)}
        with self.db.connect() as conn:
            # All dashboard panels describe the same snapshot, without reading credentials or message bodies.
            conn.execute("BEGIN")
            mailboxes = dict(
                conn.execute(
                    """SELECT COUNT(*) AS total,
                    COUNT(CASE WHEN status='active' AND expires_at>? THEN 1 END) AS active,
                    COUNT(CASE WHEN status='expired' OR (status='active' AND expires_at<=?) THEN 1 END) AS expired,
                    COUNT(CASE WHEN status='deleted' THEN 1 END) AS deleted,
                    COUNT(CASE WHEN status='active' AND expires_at>? AND last_sync_error_code IS NOT NULL THEN 1 END) AS sync_errors,
                    MAX(CASE WHEN status!='deleted' THEN last_synced_at END) AS last_synced_at
                    FROM mailboxes WHERE owner_id=?""",
                    (generated_at, generated_at, generated_at, OWNER_ID),
                ).fetchone()
            )
            messages = dict(
                conn.execute(
                    """SELECT COUNT(*) AS total,
                    COUNT(CASE WHEN m.cached_at>=? AND m.cached_at<=? THEN 1 END) AS received_24h
                    FROM messages m JOIN mailboxes b ON b.id=m.mailbox_id WHERE b.owner_id=? AND b.status!='deleted'""",
                    ((now - timedelta(hours=24)).isoformat(), generated_at, OWNER_ID),
                ).fetchone()
            )
            operations = dict.fromkeys(("pending", "running", "succeeded", "failed", "unknown"), 0)
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM operations WHERE owner_id=? GROUP BY status", (OWNER_ID,)):
                operations[row["status"]] = row["count"]
            operations["total"] = sum(operations.values())
            recent = conn.execute(
                """SELECT id, kind, status, provider_id, mailbox_id, result, error_code, created_at, updated_at
                FROM operations WHERE owner_id=? ORDER BY updated_at DESC, id DESC LIMIT 3""",
                (OWNER_ID,),
            ).fetchall()
            for row in conn.execute(
                """SELECT SUBSTR(created_at, 1, 10) AS date, COUNT(*) AS count FROM mailboxes
                WHERE owner_id=? AND created_at>=? AND created_at<=? GROUP BY date""",
                (OWNER_ID, day_start.isoformat(), generated_at),
            ):
                activity[row["date"]]["mailboxes"] = row["count"]
            for row in conn.execute(
                """SELECT SUBSTR(m.cached_at, 1, 10) AS date, COUNT(*) AS count FROM messages m
                JOIN mailboxes b ON b.id=m.mailbox_id
                WHERE b.owner_id=? AND b.status!='deleted' AND m.cached_at>=? AND m.cached_at<=? GROUP BY date""",
                (OWNER_ID, day_start.isoformat(), generated_at),
            ):
                activity[row["date"]]["messages"] = row["count"]
            providers = conn.execute(
                """SELECT provider_id AS id, COUNT(*) AS mailboxes,
                COUNT(CASE WHEN status='active' AND expires_at>? THEN 1 END) AS active
                FROM mailboxes WHERE owner_id=? GROUP BY provider_id ORDER BY mailboxes DESC, provider_id""",
                (generated_at, OWNER_ID),
            ).fetchall()
        last_synced_at = mailboxes.pop("last_synced_at")
        return {
            "generated_at": generated_at,
            "mailboxes": mailboxes,
            "messages": messages,
            "operations": operations,
            "last_synced_at": last_synced_at,
            "activity": [{"date": date, **counts} for date, counts in activity.items()],
            "recent_operations": [self.operation_view(row) for row in recent],
            "provider_stats": [dict(row) for row in providers],
        }

    def get_operation(self, operation_id: str) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=? AND owner_id=?", (operation_id, OWNER_ID)).fetchone()
        if row is None:
            raise ServiceError("OPERATION_NOT_FOUND", "Operation not found", 404)
        return self.operation_view(row)

    def submit(self, kind: str, key: str, payload: dict, mailbox_id: str | None = None) -> dict:
        if not key.strip() or len(key) > 200:
            raise ServiceError("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must contain 1 to 200 characters", 422)
        digest = hashlib.sha256(encode({"mailbox_id": mailbox_id, "payload": payload}).encode()).hexdigest()
        with self.db.connect(write=True) as conn:
            existing = conn.execute(
                "SELECT * FROM operations WHERE owner_id=? AND kind=? AND idempotency_key=?", (OWNER_ID, kind, key)
            ).fetchone()
            if existing:
                if existing["request_hash"] != digest:
                    raise ServiceError("IDEMPOTENCY_CONFLICT", "This key was used for a different request", 409)
                return self.operation_view(existing)
            try:
                if kind == "create":
                    provider = self.registry.select(payload["required_capabilities"], payload["ttl_seconds"], payload["provider"])
                else:
                    mailbox = self._mailbox(conn, mailbox_id)
                    provider = self.registry.get(mailbox["provider_id"])
                    capability = "send" if kind == "send" else "delete"
                    if not json.loads(mailbox["capabilities"])[capability] or not getattr(provider.capabilities, capability):
                        raise ServiceError("CAPABILITY_UNSUPPORTED", f"Mailbox does not support {capability}", 422)
            except ProviderError as exc:
                raise provider_error(exc) from exc
            operation_id, now = f"op_{uuid4().hex}", utcnow()
            conn.execute(
                """INSERT INTO operations
                (id, owner_id, kind, idempotency_key, request_hash, payload, provider_id, mailbox_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (operation_id, OWNER_ID, kind, key, digest, encode(payload), provider.id, mailbox_id, now, now),
            )
            return self.operation_view(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def list_messages(self, mailbox_id: str, limit: int, offset: int) -> dict:
        with self.db.connect() as conn:
            mailbox = self._mailbox(conn, mailbox_id, allow_expired=True)
            rows = conn.execute(
                """SELECT id, mailbox_id, sender, recipients, subject, received_at FROM messages
                WHERE mailbox_id=? ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?""",
                (mailbox_id, limit, offset),
            ).fetchall()
            items = [{**dict(row), "recipients": json.loads(row["recipients"])} for row in rows]
            return {"items": items, "limit": limit, "offset": offset, "last_synced_at": mailbox["last_synced_at"]}

    def get_message(self, mailbox_id: str, message_id: str) -> dict:
        with self.db.connect() as conn:
            self._mailbox(conn, mailbox_id, allow_expired=True)
            row = conn.execute("SELECT * FROM messages WHERE id=? AND mailbox_id=?", (message_id, mailbox_id)).fetchone()
        if row is None:
            raise ServiceError("MESSAGE_NOT_FOUND", "Message not found in this mailbox", 404)
        return {**dict(row), "recipients": json.loads(row["recipients"])}

    def _upstream_mailbox(self, row) -> ProviderMailbox:
        try:
            credential = self.cipher.decrypt(row["credential_encrypted"].encode()).decode()
        except (InvalidToken, AttributeError) as exc:
            raise ServiceError("CREDENTIAL_UNAVAILABLE", "Cannot decrypt mailbox credentials", 503) from exc
        return ProviderMailbox(row["upstream_id"], row["email"], credential, row["expires_at"])

    def _claim_operation(self, exclude: Collection[str] = ()):
        now = utcnow()
        cutoff = (datetime.now(UTC) - timedelta(seconds=self.settings.operation_timeout_seconds)).isoformat()
        exclusions = f" AND id NOT IN ({','.join('?' for _ in exclude)})" if exclude else ""
        with self.db.connect(write=True) as conn:
            # A dead worker may already have performed the upstream side effect. Never replay blindly.
            conn.execute(
                """UPDATE operations SET status='unknown', error_code='WORKER_INTERRUPTED', updated_at=?
                WHERE status='running' AND updated_at<?"""
                + exclusions,
                (now, cutoff, *exclude),
            )
            row = conn.execute(
                "SELECT * FROM operations WHERE status='pending'" + exclusions + " ORDER BY created_at, id LIMIT 1", tuple(exclude)
            ).fetchone()
            if row:
                conn.execute("UPDATE operations SET status='running', updated_at=? WHERE id=?", (now, row["id"]))
                return dict(row)
        return None

    def _fail_operation(self, operation_id: str, code: str, *, uncertain: bool):
        with self.db.connect(write=True) as conn:
            conn.execute(
                """UPDATE operations SET status=?, error_code=?, updated_at=? WHERE id=?
                AND (status='running' OR (status='unknown' AND error_code='WORKER_INTERRUPTED'))""",
                ("unknown" if uncertain else "failed", code, utcnow(), operation_id),
            )

    def _execute_operation(self, operation: dict):
        upstream_started = False
        try:
            provider = self.registry.get(operation["provider_id"])
            payload = json.loads(operation["payload"])
            mailbox = None
            if operation["kind"] != "create":
                with self.db.connect() as conn:
                    mailbox = dict(self._mailbox(conn, operation["mailbox_id"]))
                upstream = self._upstream_mailbox(mailbox)
                if mailbox["provider_id"] != provider.id:
                    raise ServiceError("BINDING_MISMATCH", "Operation does not match the mailbox provider", 409)
            if operation["kind"] == "create":
                self.registry.select(payload["required_capabilities"], payload["ttl_seconds"], provider.id)
                requested_expiry = datetime.now(UTC) + timedelta(seconds=payload["ttl_seconds"])
                upstream_started = True
                upstream = provider.create_mailbox(payload["ttl_seconds"], operation["id"])
                if upstream.expires_at is not None:
                    provider_expiry = datetime.fromisoformat(upstream.expires_at)
                    if provider_expiry.tzinfo is None:
                        raise ValueError("Provider expiration timestamp must include a timezone")
                    requested_expiry = min(requested_expiry, provider_expiry)
                expires_at = requested_expiry.astimezone(UTC).isoformat()
                mailbox_id = f"mb_{uuid4().hex}"
                result = {"mailbox_id": mailbox_id, "email": upstream.email}
            elif operation["kind"] == "send":
                if not provider.capabilities.send:
                    raise ServiceError("CAPABILITY_UNSUPPORTED", "Provider does not support sending", 422)
                upstream_started = True
                send_id = provider.send_message(upstream, payload["recipients"], payload["subject"], payload["text"], operation["id"])
                result = {"provider_message_id": send_id, "delivery_status": "accepted"}
            else:
                if not provider.capabilities.delete:
                    raise ServiceError("CAPABILITY_UNSUPPORTED", "Provider does not support deletion", 422)
                upstream_started = True
                provider.delete_mailbox(upstream, operation["id"])
                result = {"mailbox_id": mailbox["id"], "status": "deleted"}
            with self.db.connect(write=True) as conn:
                current = conn.execute("SELECT status, error_code FROM operations WHERE id=?", (operation["id"],)).fetchone()
                interrupted = current["status"] == "unknown" and current["error_code"] == "WORKER_INTERRUPTED"
                # Accept a late, definitive response from the original call without issuing another call.
                if current["status"] != "running" and not interrupted:
                    return
                now = utcnow()
                if operation["kind"] == "create":
                    conn.execute(
                        """INSERT INTO mailboxes
                        (id, owner_id, email, provider_id, upstream_id, credential_encrypted, capabilities,
                         status, created_at, expires_at, next_sync_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                        (
                            mailbox_id,
                            operation["owner_id"],
                            upstream.email,
                            provider.id,
                            upstream.upstream_id,
                            self.cipher.encrypt(upstream.credential.encode()).decode(),
                            encode(asdict(provider.capabilities)),
                            now,
                            expires_at,
                            now,
                        ),
                    )
                    conn.execute("UPDATE operations SET mailbox_id=? WHERE id=?", (mailbox_id, operation["id"]))
                elif operation["kind"] == "delete":
                    conn.execute("UPDATE mailboxes SET status='deleted', credential_encrypted=NULL WHERE id=?", (mailbox["id"],))
                    conn.execute("DELETE FROM messages WHERE mailbox_id=?", (mailbox["id"],))
                    conn.execute("DELETE FROM message_batches WHERE mailbox_id=?", (mailbox["id"],))
                conn.execute(
                    "UPDATE operations SET status='succeeded', result=?, error_code=NULL, updated_at=? WHERE id=?",
                    (encode(result), now, operation["id"]),
                )
        except ProviderError as exc:
            self._fail_operation(operation["id"], exc.code, uncertain=exc.uncertain)
        except ServiceError as exc:
            self._fail_operation(operation["id"], exc.code, uncertain=upstream_started)
        except TimeoutError:
            self._fail_operation(operation["id"], "PROVIDER_TIMEOUT", uncertain=upstream_started)
        except Exception:
            logger.error("Operation {} failed; upstream_started={}", operation["id"], upstream_started)
            self._fail_operation(operation["id"], "INTERNAL_ERROR", uncertain=upstream_started)

    def expire_mailboxes(self) -> int:
        with self.db.connect(write=True) as conn:
            rows = conn.execute("SELECT id FROM mailboxes WHERE status='active' AND expires_at<=?", (utcnow(),)).fetchall()
            for row in rows:
                conn.execute("UPDATE mailboxes SET status='expired', credential_encrypted=NULL WHERE id=?", (row["id"],))
            return len(rows)

    def _stage_messages(self, mailbox_id: str, provider: DestructiveReceiveProvider) -> None:
        # The caller holds the mailbox lock; HTTP must never hold SQLite's global write lock.
        with self.db.connect() as conn:
            mailbox = self._mailbox(conn, mailbox_id)
            if conn.execute("SELECT 1 FROM message_batches WHERE mailbox_id=?", (mailbox_id,)).fetchone():
                return
        payload = provider.fetch_messages(self._upstream_mailbox(mailbox))
        encrypted = self.cipher.encrypt(encode(payload).encode()).decode()
        with self.db.connect(write=True) as conn:
            self._mailbox(conn, mailbox_id)
            conn.execute(
                "INSERT INTO message_batches (mailbox_id, id, payload_encrypted, created_at) VALUES (?, ?, ?, ?)",
                (mailbox_id, uuid4().hex, encrypted, utcnow()),
            )

    def due_mailboxes(self, limit: int, exclude: Collection[str] = ()) -> list[str]:
        exclusions = f" AND id NOT IN ({','.join('?' for _ in exclude)})" if exclude else ""
        now = utcnow()
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM mailboxes WHERE status='active' AND next_sync_at<=? AND expires_at>?"
                + exclusions
                + " ORDER BY next_sync_at, id LIMIT ?",
                (now, now, *exclude, limit),
            ).fetchall()
        return [row["id"] for row in rows]

    def sync_mailbox(self, mailbox_id: str, *, scheduled: bool = False) -> bool:
        with self.db.mailbox_lock(mailbox_id) as acquired:
            if not acquired:
                return False
            return self._sync_mailbox(mailbox_id, scheduled=scheduled)

    def _sync_mailbox(self, mailbox_id: str, *, scheduled: bool) -> bool:
        try:
            with self.db.connect() as conn:
                mailbox = dict(self._mailbox(conn, mailbox_id))
            if scheduled and mailbox["next_sync_at"] > utcnow():
                return False
            provider = self.registry.get(mailbox["provider_id"])
            if not provider.capabilities.receive:
                raise ServiceError("CAPABILITY_UNSUPPORTED", "Provider does not support receiving", 422)
            if provider.capabilities.destructive_receive:
                self._stage_messages(mailbox_id, cast(DestructiveReceiveProvider, provider))
                with self.db.connect() as conn:
                    self._mailbox(conn, mailbox_id)
                    batch = conn.execute("SELECT * FROM message_batches WHERE mailbox_id=?", (mailbox_id,)).fetchone()
                if batch is None:
                    return False
                payload = json.loads(self.cipher.decrypt(batch["payload_encrypted"].encode()))
                messages = cast(DestructiveReceiveProvider, provider).parse_messages(self._upstream_mailbox(mailbox), payload, batch["id"])
            else:
                messages = provider.list_messages(self._upstream_mailbox(mailbox))
            now = utcnow()
            next_sync = (datetime.now(UTC) + timedelta(seconds=self.settings.sync_interval_seconds)).isoformat()
            with self.db.connect(write=True) as conn:
                # Deletion or expiry during the upstream request must not recreate the local cache.
                self._mailbox(conn, mailbox_id)
                conn.executemany(
                    """INSERT INTO messages (id, mailbox_id, upstream_id, sender, recipients, subject, text, received_at, cached_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(mailbox_id, upstream_id) DO NOTHING""",
                    [
                        (
                            f"msg_{uuid4().hex}",
                            mailbox_id,
                            msg.upstream_id,
                            msg.sender,
                            encode(msg.recipients),
                            msg.subject,
                            msg.text,
                            msg.received_at,
                            now,
                        )
                        for msg in messages
                    ],
                )
                conn.execute(
                    """UPDATE mailboxes SET last_synced_at=?, next_sync_at=?, sync_failures=0, last_sync_error_code=NULL WHERE id=?""",
                    (now, next_sync, mailbox_id),
                )
                conn.execute("DELETE FROM message_batches WHERE mailbox_id=?", (mailbox_id,))
            return True
        except (ProviderError, ServiceError) as exc:
            code = exc.code
        except TimeoutError:
            code = "PROVIDER_TIMEOUT"
        except Exception:
            logger.error("Mailbox sync failed for {}", mailbox_id)
            code = "INTERNAL_ERROR"
        with self.db.connect(write=True) as conn:
            row = conn.execute("SELECT sync_failures FROM mailboxes WHERE id=? AND status='active'", (mailbox_id,)).fetchone()
            if row:
                failures = row["sync_failures"] + 1
                delay = min(self.settings.sync_interval_seconds * 2 ** min(failures, 10), 3600)
                next_sync = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
                conn.execute(
                    "UPDATE mailboxes SET sync_failures=?, last_sync_error_code=?, next_sync_at=? WHERE id=?",
                    (failures, code, next_sync, mailbox_id),
                )
        return False

    def run_once(self, refresh: Callable[[], "Service"] | None = None) -> dict:
        expired = self.expire_mailboxes()
        operation = self._claim_operation()
        # A newly claimed job may use a provider configured after this tick started.
        active = refresh() if refresh is not None else self
        if operation:
            active._execute_operation(operation)
        synced = sum(active.sync_mailbox(mailbox_id, scheduled=True) for mailbox_id in active.due_mailboxes(20))
        return {"operations": int(operation is not None), "synced": synced, "expired": expired}
