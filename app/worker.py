import argparse
import signal
import sqlite3
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

from loguru import logger

from app.config import Settings
from app.limits import MAX_WORKER_CONCURRENCY
from app.logging import configure_logging
from app.runtime import Runtime
from app.service import Service


def wait_for_next_tick(runtime: Runtime, wake: Event | None = None, stop: Event | None = None, previous: Service | None = None):
    started = time.monotonic()
    previous = previous or runtime.refresh()
    while True:
        if stop is not None and stop.is_set():
            return
        service = runtime.refresh()
        if wake is not None and service is not previous:
            return
        remaining = service.settings.worker_poll_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return
        if wake is None:
            time.sleep(min(1, remaining))
        elif wake.wait(min(1, remaining)):
            return


class Worker:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.snapshot = runtime.refresh()
        self.wake = Event()
        self.stop = Event()
        # Threads are created lazily. Admission limits can change without abandoning running jobs.
        self.operations = ThreadPoolExecutor(max_workers=MAX_WORKER_CONCURRENCY, thread_name_prefix="mail-operation")
        self.receivers = ThreadPoolExecutor(max_workers=MAX_WORKER_CONCURRENCY, thread_name_prefix="mail-receive")
        self.operation_jobs: dict[Future, str] = {}
        self.receive_jobs: dict[Future, str] = {}
        self.receive_retry_at: dict[str, float] = {}

    def request_stop(self, *_args):
        self.stop.set()
        self.wake.set()

    def _execute(self, operation: dict):
        # Read one immutable snapshot when the task starts, then retain it until completion.
        self.runtime.refresh()._execute_operation(operation)

    def _receive(self, mailbox_id: str) -> bool:
        return self.runtime.refresh().sync_mailbox(mailbox_id, scheduled=True)

    def collect(self, *, strict: bool = False) -> dict:
        result = {"operations": 0, "synced": 0, "expired": 0}
        for jobs, kind in ((self.operation_jobs, "operations"), (self.receive_jobs, "synced")):
            for future, identifier in list(jobs.items()):
                if not future.done():
                    continue
                del jobs[future]
                try:
                    value = future.result()
                    result[kind] += 1 if kind == "operations" else int(value)
                    if kind == "synced" and not value:
                        self.receive_retry_at[identifier] = time.monotonic() + self.snapshot.settings.worker_poll_seconds
                except Exception:
                    # Keep uncertain operations in storage for recovery; never replay upstream effects here.
                    logger.error("Worker {} task {} failed; its persisted state will be checked on a later tick", kind, identifier)
                    if kind == "synced":
                        self.receive_retry_at[identifier] = time.monotonic() + self.snapshot.settings.worker_poll_seconds
                    if strict:
                        raise
        return result

    def tick(self) -> dict:
        self.wake.clear()
        result = self.collect()
        self.snapshot = self.runtime.refresh()
        if self.stop.is_set():
            return result
        result["expired"] = self.snapshot.expire_mailboxes()
        # Never queue more work than the configured concurrency: reducing a limit simply stops admission.
        while not self.stop.is_set():
            service = self.runtime.refresh()
            if len(self.operation_jobs) >= service.settings.create_concurrency:
                break
            operation = service._claim_operation(exclude=tuple(self.operation_jobs.values()))
            if operation is None:
                break
            future = self.operations.submit(self._execute, operation)
            self.operation_jobs[future] = operation["id"]
            future.add_done_callback(lambda _future: self.wake.set())
        service = self.runtime.refresh()
        self.receive_retry_at = {key: deadline for key, deadline in self.receive_retry_at.items() if deadline > time.monotonic()}
        available = max(0, service.settings.receive_concurrency - len(self.receive_jobs))
        if available and not self.stop.is_set():
            excluded = (*self.receive_jobs.values(), *self.receive_retry_at)
            for mailbox_id in service.due_mailboxes(available, exclude=excluded):
                if self.stop.is_set() or len(self.receive_jobs) >= self.runtime.refresh().settings.receive_concurrency:
                    break
                future = self.receivers.submit(self._receive, mailbox_id)
                self.receive_jobs[future] = mailbox_id
                future.add_done_callback(lambda _future: self.wake.set())
        return result

    def close(self, *, strict: bool = False) -> dict:
        self.request_stop()
        # Do not cancel claimed jobs: their network request may already have reached the provider.
        self.operations.shutdown(wait=True)
        self.receivers.shutdown(wait=True)
        return self.collect(strict=strict)


def log_result(result: dict):
    if any(result.values()):
        logger.info("Worker tick: {}", result)


def main():
    parser = argparse.ArgumentParser(description="Process mailbox operations, synchronize messages, and expire local mailboxes")
    parser.add_argument("--once", action="store_true", help="Run one bounded batch concurrently, wait for it, and exit")
    args = parser.parse_args()
    configure_logging()
    runtime = Runtime(Settings.from_yaml())
    worker = Worker(runtime)
    handlers = {sig: signal.signal(sig, worker.request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        while not worker.stop.is_set():
            try:
                log_result(worker.tick())
            except sqlite3.OperationalError:
                if args.once:
                    raise
                logger.error("Worker storage is temporarily unavailable; retrying on the next tick")
            if args.once:
                return
            wait_for_next_tick(runtime, worker.wake, worker.stop, worker.snapshot)
    except KeyboardInterrupt:
        worker.request_stop()
    finally:
        try:
            log_result(worker.close(strict=args.once))
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            logger.info("Worker stopped")


if __name__ == "__main__":
    main()
