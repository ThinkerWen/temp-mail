import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml
from cryptography.fernet import Fernet

import run as launcher
from app import server
from app.db import Database, utcnow
from app.logging import UVICORN_LOG_CONFIG

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_project(tmp_path):
    shutil.copy2(PROJECT_ROOT / "run.py", tmp_path / "run.py")
    shutil.copy2(PROJECT_ROOT / "main.py", tmp_path / "main.py")
    shutil.copytree(PROJECT_ROOT / "app", tmp_path / "app", ignore=shutil.ignore_patterns("utils.py", "__pycache__"))
    config = {
        "app": {"db_path": "data/test.db", "api_token": "isolated-launcher-test-token", "encryption_key": Fernet.generate_key().decode()},
        "worker": {"poll_seconds": 0.1},
        "providers": {},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    return tmp_path


def wait_until(predicate, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Condition did not become true before timeout")


def process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def child_pids(log):
    return {int(pid) for pid in re.findall(r"(?:PID |server process \[)(\d+)", log)}


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group and signal integration checks")
@pytest.mark.parametrize("reload_api, shutdown_signal", [(False, signal.SIGINT), (True, signal.SIGTERM)])
def test_real_api_and_worker_process_queue_and_stop_together(isolated_project, reload_api, shutdown_signal):
    database = Database(str(isolated_project / "data" / "test.db"))
    database.initialize()
    with database.connect(write=True) as conn:
        conn.execute(
            """INSERT INTO operations
            (id, owner_id, kind, idempotency_key, request_hash, payload, provider_id, status, created_at, updated_at)
            VALUES ('op_launcher', 'local', 'create', 'launcher', 'test', '{}', 'unavailable-test-provider', 'pending', ?, ?)""",
            (utcnow(), utcnow()),
        )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log_path = isolated_project / "launcher.log"
    arguments = [sys.executable, str(isolated_project / "run.py"), "--port", str(port)]
    if reload_api:
        arguments.append("--reload")
    # Start outside the project to check that the launcher controls the children's working directory.
    with log_path.open("w") as log_file:
        process = subprocess.Popen(
            arguments, cwd=isolated_project.parent, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            wait_until(lambda: "Started Worker" in log_path.read_text() or process.poll() is not None)
            assert process.poll() is None, log_path.read_text()
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
                assert client.get("/health/ready").status_code == 200

                def operation_processed():
                    with database.connect() as conn:
                        row = conn.execute("SELECT status, error_code FROM operations WHERE id='op_launcher'").fetchone()
                        return tuple(row) == ("failed", "PROVIDER_UNAVAILABLE")

                wait_until(operation_processed)
                if reload_api:
                    wait_until(lambda: "Started server process" in log_path.read_text())
                    assert "Started reloader process" in log_path.read_text()
                    time.sleep(0.6)  # Let the reloader establish its initial file timestamps.
                    config_module = isolated_project / "app" / "config.py"
                    config_module.write_text(config_module.read_text() + "\n# Trigger a development reload.\n")
                    wait_until(lambda: log_path.read_text().count("Started server process") >= 2)
                    assert log_path.read_text().count("Started Worker") == 1
                    assert client.get("/health/ready").status_code == 200
                pids = child_pids(log_path.read_text())
                assert len(pids) >= (3 if reload_api else 2)
                process.send_signal(shutdown_signal)
                assert process.wait(timeout=20) == 0, log_path.read_text()
                wait_until(lambda: all(not process_exists(pid) for pid in pids), timeout=5)
                log = log_path.read_text()
                for marker in ["Starting API", "Started Worker", "Started server process", "Worker tick:", "GET /health/ready HTTP/1.1"]:
                    entries = [line for line in log.splitlines() if marker in line]
                    assert entries, log
                    assert all(re.match(r"^\d{4}-\d{2}-\d{2} .* \| INFO\s* \| PID \d+ \| ", line) for line in entries), log
                assert log.count("Started Worker") == 1, log
                assert log.count("Started server process") == (2 if reload_api else 1), log
                if reload_api:
                    entries = [line for line in log.splitlines() if "Started reloader process" in line]
                    assert len(entries) == 1, log
                    assert "| INFO" in entries[0] and "| PID " in entries[0], log
                with pytest.raises(httpx.ConnectError):
                    client.get("/health/ready")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for pid in child_pids(log_path.read_text()):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_occupied_port_fails_before_starting_worker(isolated_project):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = subprocess.run(
            [sys.executable, str(isolated_project / "run.py"), "--port", str(listener.getsockname()[1])],
            cwd=isolated_project.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode == 1
    assert "Cannot bind" in result.stderr
    assert "Started Worker" not in result.stderr
    assert not (isolated_project / "data").exists()


@pytest.mark.parametrize("service, exit_code", [("API", 0), ("API", 3), ("Worker", 0), ("Worker", 7), ("Worker", -9)])
def test_unexpected_service_exit_stops_peer_and_returns_failure(monkeypatch, service, exit_code):
    api = Mock(pid=1001, returncode=exit_code if service == "API" else None)
    worker = Mock(pid=1002, returncode=exit_code if service == "Worker" else None)
    api.poll.return_value = api.returncode
    worker.poll.return_value = worker.returncode
    monkeypatch.setattr(launcher.Settings, "from_yaml", Mock())
    monkeypatch.setattr(launcher, "check_port", Mock())
    monkeypatch.setattr(launcher, "wait_for_api", Mock(return_value=True))
    monkeypatch.setattr(launcher, "start_process", Mock(side_effect=[api, worker]))
    stop_processes = Mock()
    monkeypatch.setattr(launcher, "stop_processes", stop_processes)
    assert launcher.main([]) == max(1, exit_code)
    stop_processes.assert_called_once_with([("API", api), ("Worker", worker)])


def test_api_startup_failure_does_not_start_worker(monkeypatch):
    api = Mock(pid=1001)
    monkeypatch.setattr(launcher.Settings, "from_yaml", Mock())
    monkeypatch.setattr(launcher, "check_port", Mock())
    monkeypatch.setattr(launcher, "wait_for_api", Mock(return_value=False))
    start_process = Mock(return_value=api)
    stop_processes = Mock()
    monkeypatch.setattr(launcher, "start_process", start_process)
    monkeypatch.setattr(launcher, "stop_processes", stop_processes)
    assert launcher.main([]) == 1
    assert start_process.call_count == 1
    stop_processes.assert_called_once_with([("API", api)])


@pytest.fixture
def uvicorn_run(monkeypatch):
    run = Mock()
    monkeypatch.setattr(server.uvicorn, "run", run)
    return run


def test_server_defaults_use_python_log_config(uvicorn_run):
    server.main([])
    uvicorn_run.assert_called_once()
    args, kwargs = uvicorn_run.call_args
    assert args == ("main:app",)
    assert kwargs["log_config"] is UVICORN_LOG_CONFIG
    assert isinstance(kwargs["log_config"], dict)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8000
    assert kwargs["reload"] is False
    assert kwargs.get("reload_dirs") is None
    assert kwargs.get("timeout_graceful_shutdown") is None


def test_server_forwards_custom_options_and_all_reload_directories(uvicorn_run):
    server.main(
        [
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
            "--reload",
            "--reload-dir",
            "app",
            "--reload-dir",
            "tests",
            "--timeout-graceful-shutdown",
            "7",
        ]
    )
    uvicorn_run.assert_called_once()
    args, kwargs = uvicorn_run.call_args
    assert args == ("main:app",)
    assert kwargs["log_config"] is UVICORN_LOG_CONFIG
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 8123
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == ["app", "tests"]
    assert kwargs["timeout_graceful_shutdown"] == 7


@pytest.mark.parametrize("port", ["0", "65536", "-1", "invalid"])
def test_server_rejects_invalid_ports_before_starting_uvicorn(uvicorn_run, port):
    with pytest.raises(SystemExit) as exc:
        server.main(["--port", port])
    assert exc.value.code == 2
    uvicorn_run.assert_not_called()


@pytest.mark.parametrize("port", ["1", "65535"])
def test_server_accepts_port_boundaries(uvicorn_run, port):
    server.main(["--port", port])
    assert uvicorn_run.call_args.kwargs["port"] == int(port)


@pytest.fixture
def docker_launcher(isolated_project):
    scripts = isolated_project / "scripts"
    scripts.mkdir()
    for name in ("start.sh", "init_config.py"):
        shutil.copy2(PROJECT_ROOT / "scripts" / name, scripts / name)
    shutil.copy2(isolated_project / "config.yaml", isolated_project / "config.example.yaml")
    config_path = isolated_project / "config" / "config.yaml"

    @contextmanager
    def start():
        database = Database(str(config_path.parent / "data" / "test.db"))
        database.initialize()
        with database.connect(write=True) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO operations
                (id, owner_id, kind, idempotency_key, request_hash, payload, provider_id, status, created_at, updated_at)
                VALUES ('op_docker', 'local', 'create', 'docker', 'test', '{}', 'unavailable-test-provider', 'pending', ?, ?)""",
                (utcnow(), utcnow()),
            )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        log_path = isolated_project / "docker.log"
        env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", "PORT": str(port)}
        env["TEMP_MAIL_CONFIG"] = str(config_path)
        with log_path.open("w") as log_file:
            process = subprocess.Popen(
                ["bash", str(scripts / "start.sh")],
                cwd=isolated_project.parent,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                try:
                    wait_until(lambda: "Worker tick:" in log_path.read_text() or process.poll() is not None)
                except AssertionError as exc:
                    raise AssertionError(log_path.read_text()) from exc
                log = log_path.read_text()
                assert process.poll() is None, log
                api_pid = int(re.search(r"Started server process \[(\d+)\]", log)[1])
                worker_pid = int(re.search(r"PID (\d+) \| .* - Worker tick:", log)[1])
                yield process, port, {"api": api_pid, "worker": worker_pid}, config_path, log_path
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    return start


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell and signal integration checks")
@pytest.mark.parametrize("shutdown", ["SIGTERM", "api", "worker"])
def test_docker_starts_both_services_and_cleans_up_on_exit(docker_launcher, shutdown):
    with docker_launcher() as (process, port, pids, config_path, log_path):
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
            assert client.get("/health/ready").status_code == 200
            database = Database(str(config_path.parent / "data" / "test.db"))

            def operation_processed():
                with database.connect() as conn:
                    row = conn.execute("SELECT status, error_code FROM operations WHERE id='op_docker'").fetchone()
                    return tuple(row) == ("failed", "PROVIDER_UNAVAILABLE")

            wait_until(operation_processed)
            if shutdown == "SIGTERM":
                process.terminate()
                assert process.wait(timeout=20) == 0, log_path.read_text()
            else:
                os.kill(pids[shutdown], signal.SIGKILL)
                assert process.wait(timeout=20) != 0, log_path.read_text()
            wait_until(lambda: all(not process_exists(pid) for pid in pids.values()), timeout=5)
            with pytest.raises(httpx.ConnectError):
                client.get("/health/ready")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell integration check")
def test_docker_initializes_config_once_and_preserves_credentials(docker_launcher):
    with docker_launcher() as (_, port, _, config_path, _):
        original = config_path.read_bytes()
        config = yaml.safe_load(original)
        token = config["app"]["api_token"]
        assert token == "default-access-api-token"
        Fernet(config["app"]["encryption_key"].encode())
        assert config_path.stat().st_mode & 0o777 == 0o600
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
            response = client.get("/v1/mailboxes", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["items"] == []
    with docker_launcher() as (_, port, _, config_path, log_path):
        assert config_path.read_bytes() == original
        assert "Created configuration" not in log_path.read_text()
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
            response = client.get("/v1/mailboxes", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["items"] == []
