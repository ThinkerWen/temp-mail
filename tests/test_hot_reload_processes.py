"""Exercise independent API/Worker processes against an isolated local provider."""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Condition did not become true before timeout")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group integration")
def test_saved_configuration_reaches_running_api_and_worker_without_restart(tmp_path):
    calls = []

    class Upstream(BaseHTTPRequestHandler):
        def respond(self, value):
            payload = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            assert self.path in {"/first/mailbox", "/second/mailbox"}
            calls.append(self.path)
            identifier = len(calls)
            self.respond({"token": f"local-mailbox-{identifier}", "mailbox": f"mailbox-{identifier}@example.test"})

        def do_GET(self):
            identifier = self.headers["Authorization"].removeprefix("Bearer local-mailbox-")
            self.respond({"mailbox": f"mailbox-{identifier}@example.test", "messages": []})

        def log_message(self, *_args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    token = "isolated-live-reload-token-12345"
    config = {
        "app": {"db_path": str(tmp_path / "mail.db"), "api_token": token, "encryption_key": Fernet.generate_key().decode()},
        "worker": {"poll_seconds": 120},
        "providers": {},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log_path = tmp_path / "processes.log"
    project = Path(__file__).resolve().parents[1]
    process = None
    try:
        with log_path.open("w") as logs:
            process = subprocess.Popen(
                [sys.executable, str(project / "run.py"), "--port", str(port)],
                cwd=tmp_path,
                env={**os.environ, "TEMP_MAIL_CONFIG": str(config_path)},
                stdout=logs,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            wait_for(lambda: "Started Worker" in log_path.read_text() or process.poll() is not None)
            assert process.poll() is None, log_path.read_text()
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {token}"}, trust_env=False) as api:
                assert api.get("/v1/capabilities").json() == {"providers": []}
                # Let the worker start its originally long idle wait before changing the file.
                time.sleep(0.3)

                def save(**changes):
                    revision = api.get("/v1/config").json()["revision"]
                    response = api.put("/v1/config", json={"revision": revision, **changes})
                    assert response.status_code == 200, response.text
                    return response.json()

                provider = {
                    "id": "temp-mail-org",
                    "enabled": True,
                    "index_url": None,
                    "base_url": f"http://127.0.0.1:{upstream.server_port}/first",
                    "impersonate": "chrome110",
                    "timeout_seconds": 2,
                    "max_ttl_seconds": 600,
                }
                saved = save(providers=[provider], worker={"poll_seconds": 0.1})
                assert saved["restart_required"] is False
                assert saved["restart_required_fields"] == []
                assert api.get("/v1/capabilities").json()["providers"][0]["capabilities"]["max_ttl_seconds"] == 600

                def create(key, ttl):
                    response = api.post(
                        "/v1/mailboxes", json={"provider": "temp-mail-org", "ttl_seconds": ttl}, headers={"Idempotency-Key": key}
                    )
                    assert response.status_code == 202, response.text
                    operation_id = response.json()["id"]
                    wait_for(lambda: api.get(f"/v1/operations/{operation_id}").json()["status"] == "succeeded", timeout=8)
                    return api.get(f"/v1/operations/{operation_id}").json()["mailbox_id"]

                first = create("first", 600)
                expiry = api.get(f"/v1/mailboxes/{first}").json()["expires_at"]
                provider.update(base_url=f"http://127.0.0.1:{upstream.server_port}/second", max_ttl_seconds=1200)
                save(providers=[provider])
                assert api.get("/v1/capabilities").json()["providers"][0]["capabilities"]["max_ttl_seconds"] == 1200
                create("second", 1200)
                assert calls == ["/first/mailbox", "/second/mailbox"]
                assert api.get(f"/v1/mailboxes/{first}").json()["expires_at"] == expiry
                provider["enabled"] = False
                assert save(providers=[provider])["restart_required"] is False
                assert api.get("/v1/capabilities").json() == {"providers": []}
                assert api.post("/v1/mailboxes", json={"ttl_seconds": 600}, headers={"Idempotency-Key": "disabled"}).status_code == 503
                replacement_token = "isolated-replacement-token-12345"
                assert save(app={"api_token": replacement_token})["restart_required"] is False
                assert api.get("/v1/mailboxes").status_code == 401
                api.headers["Authorization"] = f"Bearer {replacement_token}"
                assert api.get("/v1/mailboxes").status_code == 200
                pending_path = tmp_path / "pending" / "other.db"
                saved = save(app={"db_path": str(pending_path)}, worker={"sync_interval_seconds": 9})
                assert saved["restart_required_fields"] == ["app.db_path"]
                assert len(api.get("/v1/mailboxes").json()["items"]) == 2
                assert not pending_path.exists()
            process.send_signal(signal.SIGINT)
            assert process.wait(timeout=15) == 0, log_path.read_text()
            log = log_path.read_text()
            assert log.count("Started Worker") == 1
            assert log.count("Started server process") == 1
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log_path.exists():
            for pid in set(re.findall(r"(?:PID |server process \[)(\d+)", log_path.read_text())):
                try:
                    os.killpg(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)
