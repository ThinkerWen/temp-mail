"""Run the local API and one worker under a shared process supervisor."""

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from loguru import logger

from app.config import Settings
from app.logging import configure_logging

PROJECT_ROOT = Path(__file__).resolve().parent


def start_process(arguments: list[str]) -> subprocess.Popen:
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen([sys.executable, *arguments], cwd=PROJECT_ROOT, **options)


def signal_process(process: subprocess.Popen, *, force: bool = False):
    try:
        if os.name != "nt":
            # Uvicorn's reload child belongs to the same group, even if its parent has exited.
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGINT)
        elif process.poll() is None:
            if force:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                process.send_signal(signal.CTRL_BREAK_EVENT)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.kill() if force else process.terminate()


def stop_processes(processes: list[tuple[str, subprocess.Popen]], timeout: float = 15):
    for _, process in reversed(processes):
        signal_process(process)
    deadline = time.monotonic() + timeout
    for name, process in reversed(processes):
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            logger.warning("{} did not stop in time; terminating its process group", name)
    for _, process in processes:
        signal_process(process, force=True)
        process.wait()


def check_port(host: str, port: int):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    addresses = socket.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
    family, socktype, proto, _, address = addresses[0]
    with socket.socket(family, socktype, proto) as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(address)


def wait_for_api(process: subprocess.Popen, host: str, port: int, stop: threading.Event, timeout: float = 30) -> bool:
    target = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    authority = f"[{target}]" if ":" in target else target
    # Local readiness must not be sent through a shell's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while not stop.wait(0.1):
        if process.poll() is not None:
            logger.error("API exited before becoming ready (exit code {})", process.returncode)
            return False
        try:
            with opener.open(f"http://{authority}:{port}/health/ready", timeout=0.5) as response:
                if response.status == 200 and process.poll() is None:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            logger.error("API did not become ready within {} seconds", timeout)
            return False
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the local API and one worker; Ctrl+C stops both")
    parser.add_argument("--host", default="127.0.0.1", help="API bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="API port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Reload API code on changes; worker is not reloaded")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    configure_logging()
    try:
        Settings.from_yaml(PROJECT_ROOT / os.environ.get("TEMP_MAIL_CONFIG", "config.yaml"))
    except (OSError, ValueError):
        logger.error("Cannot read project config.yaml; initialize it with: uv run python scripts/init_config.py")
        return 1
    try:
        check_port(args.host, args.port)
    except OSError:
        logger.error("Cannot bind {}:{}; stop the existing API or choose another --port", args.host, args.port)
        return 1

    stop = threading.Event()
    signals = [signal.SIGINT, signal.SIGTERM]
    if os.name == "nt":
        signals.append(signal.SIGBREAK)
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in signals}
    processes: list[tuple[str, subprocess.Popen]] = []
    try:
        command = ["-m", "app.server", "--host", args.host, "--port", str(args.port), "--timeout-graceful-shutdown", "10"]
        if args.reload:
            command.extend(["--reload", "--reload-dir", str(PROJECT_ROOT / "app")])
        api_process = start_process(command)
        processes.append(("API", api_process))
        logger.info("Starting API (PID {})", api_process.pid)
        if not wait_for_api(api_process, args.host, args.port, stop):
            return 0 if stop.is_set() else 1
        if stop.is_set():
            return 0
        worker = start_process(["-m", "app.worker"])
        processes.append(("Worker", worker))
        logger.info("Started Worker (PID {}). Press Ctrl+C to stop both services.", worker.pid)
        while not stop.wait(0.2):
            for name, process in processes:
                if process.poll() is not None:
                    logger.error("{} exited unexpectedly (exit code {}); stopping both services", name, process.returncode)
                    return process.returncode if process.returncode > 0 else 1
        return 0
    except OSError:
        logger.error("Cannot start a service; check the Python environment and installed dependencies")
        return 1
    finally:
        logger.info("Stopping local services")
        try:
            stop_processes(processes)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
