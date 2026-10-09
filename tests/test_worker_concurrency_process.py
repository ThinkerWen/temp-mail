"""Verify live concurrency changes in an independent Worker against a local HTTP server."""

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from app.config import Settings
from app.config_store import ConfigStore
from app.service import Service


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.02)
    raise AssertionError("Worker did not reach the expected state before timeout")


@pytest.mark.skipif(os.name == "nt", reason="Requires graceful POSIX SIGTERM shutdown")
def test_worker_process_hot_resizes_independent_creation_and_receiving_limits(tmp_path):
    changed = threading.Condition()
    gates = {"POST": [], "GET": []}
    active = {"POST": set(), "GET": set()}
    errors = []
    releasing = threading.Event()

    class Upstream(BaseHTTPRequestHandler):
        def respond(self):
            method = self.command
            with changed:
                index = len(gates[method])
                gate = threading.Event()
                gates[method].append(gate)
                active[method].add(index)
                if releasing.is_set():
                    gate.set()
                changed.notify_all()
            try:
                if not gate.wait(20):
                    raise TimeoutError("Test did not release a blocked upstream request")
                if method == "POST":
                    assert self.path == "/mailbox"
                    value = {"token": f"created-{index}", "mailbox": f"created-{index}@example.test"}
                else:
                    assert self.path == "/messages"
                    identifier = self.headers["Authorization"].removeprefix("Bearer ")
                    value = {"mailbox": f"{identifier}@example.test", "messages": []}
                payload = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception as exc:
                with changed:
                    errors.append(repr(exc))
            finally:
                with changed:
                    active[method].discard(index)
                    changed.notify_all()

        do_POST = respond
        do_GET = respond

        def log_message(self, *_args):
            pass

    def counts():
        return len(gates["POST"]), len(gates["GET"])

    def wait_counts(expected):
        with changed:
            assert changed.wait_for(lambda: counts() == expected or bool(errors), timeout=10), (counts(), expected)
            assert not errors
            assert counts() == expected

    def assert_no_admission(expected):
        with changed:
            assert counts() == expected
            # Several scheduling ticks must leave occupied slots untouched, even with pending work.
            assert not changed.wait_for(lambda: counts() != expected or bool(errors), timeout=0.3), (counts(), errors)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    config_path = tmp_path / "config.yaml"
    document = {
        "app": {
            "db_path": str(tmp_path / "mail.sqlite"),
            "api_token": "isolated-worker-concurrency-test-token",
            "encryption_key": Fernet.generate_key().decode(),
        },
        "worker": {
            "poll_seconds": 0.05,
            "sync_interval_seconds": 3600,
            "create_concurrency": 1,
            "receive_concurrency": 1,
        },
        "providers": {
            "temp-mail-org": {
                "base_url": f"http://127.0.0.1:{upstream.server_port}",
                "timeout_seconds": 25,
                "impersonate": "chrome110",
            },
        },
    }
    config_path.write_text(yaml.safe_dump(document))
    settings = Settings.from_yaml(config_path)
    service = Service(settings)
    capabilities = json.dumps(asdict(service.registry.get("temp-mail-org").capabilities))
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with service.db.connect(write=True) as conn:
        conn.executemany(
            """INSERT INTO mailboxes
            (id, owner_id, email, provider_id, upstream_id, credential_encrypted, capabilities,
             status, created_at, expires_at, next_sync_at)
            VALUES (?, 'default', ?, 'temp-mail-org', ?, ?, ?, 'active', ?, ?, ?)""",
            [
                (
                    f"seed-{index}",
                    f"seed-{index}@example.test",
                    f"seed-{index}",
                    service.cipher.encrypt(f"seed-{index}".encode()).decode(),
                    capabilities,
                    "2000-01-01T00:00:00+00:00",
                    expiry,
                    "2000-01-01T00:00:00+00:00",
                )
                for index in range(6)
            ],
        )
    operations = [
        service.submit(
            "create", f"create-{index}", {"provider": "temp-mail-org", "required_capabilities": ["receive"], "ttl_seconds": 600}
        )["id"]
        for index in range(5)
    ]
    store = ConfigStore(settings)

    def resize(create, receive):
        saved = store.save({"revision": store.get()["revision"], "worker": {"create_concurrency": create, "receive_concurrency": receive}})
        assert saved["restart_required"] is False

    def completed():
        with service.db.connect() as conn:
            created = conn.execute("SELECT COUNT(*) FROM operations WHERE status='succeeded'").fetchone()[0]
            received = conn.execute("SELECT COUNT(*) FROM mailboxes WHERE id LIKE 'seed-%' AND last_synced_at IS NOT NULL").fetchone()[0]
        return created, received

    log_path = tmp_path / "worker.log"
    process = None
    try:
        with log_path.open("w") as logs:
            process = subprocess.Popen(
                [sys.executable, "-m", "app.worker"],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "TEMP_MAIL_CONFIG": str(config_path), "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"},
                stdout=logs,
                stderr=subprocess.STDOUT,
            )
            wait_counts((1, 1))
            assert_no_admission((1, 1))
            resize(2, 3)
            wait_counts((2, 3))
            with changed:
                assert active == {"POST": {0, 1}, "GET": {0, 1, 2}}
                assert not any(gate.is_set() for values in gates.values() for gate in values)
            assert completed() == (0, 0)

            resize(1, 1)
            assert_no_admission((2, 3))
            gates["POST"][0].set()
            gates["GET"][0].set()
            gates["GET"][1].set()
            wait_for(lambda: completed() == (1, 2))
            assert_no_admission((2, 3))
            with changed:
                assert active == {"POST": {1}, "GET": {2}}

            # An available creation slot must refill while the last receiving request remains blocked.
            gates["POST"][1].set()
            wait_counts((3, 3))
            assert_no_admission((3, 3))
            gates["GET"][2].set()
            wait_counts((3, 4))
            assert_no_admission((3, 4))
            with changed:
                assert active == {"POST": {2}, "GET": {3}}
                releasing.set()
                for values in gates.values():
                    for gate in values:
                        gate.set()
            wait_for(lambda: completed() == (5, 6))
            assert all(service.get_operation(identifier)["status"] == "succeeded" for identifier in operations)
            assert not errors
            assert process.poll() is None, log_path.read_text()
            process.terminate()
            assert process.wait(timeout=10) == 0, log_path.read_text()
    finally:
        with changed:
            releasing.set()
            for values in gates.values():
                for gate in values:
                    gate.set()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
