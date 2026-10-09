import sqlite3
from dataclasses import replace
from threading import Condition, Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet

from app import worker
from app.config import Settings
from app.providers.registry import Registry
from app.runtime import Runtime
from app.worker import Worker, wait_for_next_tick
from tests.fakes import FakeProvider


class Gates:
    def __init__(self, identifiers):
        self.gates = {key: Event() for key in identifiers}
        self.entered = []
        self.condition = Condition()

    def enter(self, key):
        with self.condition:
            self.entered.append(key)
            self.condition.notify_all()
        assert self.gates[key].wait(10)

    def wait(self, count):
        with self.condition:
            assert self.condition.wait_for(lambda: len(self.entered) >= count, timeout=5)

    def release(self):
        for gate in self.gates.values():
            gate.set()


@pytest.fixture
def setup(tmp_path):
    provider = FakeProvider("scheduler", str(tmp_path / "upstream.db"))
    settings = Settings(
        str(tmp_path / "gateway.db"),
        "scheduler-test-token-not-real",
        Fernet.generate_key().decode(),
        create_concurrency=2,
        receive_concurrency=2,
        sync_interval_seconds=3600,
    )
    runtime = Runtime(settings, Registry([provider]))
    return runtime, provider


def enqueue(runtime, count):
    return [
        runtime.service.submit(
            "create", f"create-{i}", {"provider": "scheduler", "ttl_seconds": 3600, "required_capabilities": ["receive"]}
        )
        for i in range(count)
    ]


def finish(jobs, identifier):
    next(future for future, key in jobs.items() if key == identifier).result(timeout=5)


def test_creation_slots_refill_without_batch_barrier_and_decrease_drains_inflight(setup, monkeypatch):
    runtime, provider = setup
    operations = enqueue(runtime, 4)
    identifiers = [item["id"] for item in operations]
    gates = Gates(identifiers)
    create = provider.create_mailbox

    def blocked_create(ttl, request_id):
        gates.enter(request_id)
        return create(ttl, request_id)

    monkeypatch.setattr(provider, "create_mailbox", blocked_create)
    scheduler = Worker(runtime)
    try:
        scheduler.tick()
        gates.wait(2)
        scheduler.tick()
        assert len(scheduler.operation_jobs) == len(gates.entered) == 2
        assert [runtime.service.get_operation(key)["status"] for key in identifiers] == ["running", "running", "pending", "pending"]
        gates.gates[identifiers[0]].set()
        finish(scheduler.operation_jobs, identifiers[0])
        scheduler.tick()
        gates.wait(3)
        assert len(scheduler.operation_jobs) == 2
        assert identifiers[1] in scheduler.operation_jobs.values()
        runtime.prepare(replace(runtime.service.settings, create_concurrency=1), "lower")()
        scheduler.tick()
        assert len(gates.entered) == 3
        gates.gates[identifiers[1]].set()
        finish(scheduler.operation_jobs, identifiers[1])
        scheduler.tick()
        assert len(scheduler.operation_jobs) == 1
        assert runtime.service.get_operation(identifiers[3])["status"] == "pending"
        gates.gates[identifiers[2]].set()
        finish(scheduler.operation_jobs, identifiers[2])
        scheduler.tick()
        gates.wait(4)
        assert len(scheduler.operation_jobs) == 1
        assert sorted(gates.entered) == sorted(identifiers)
    finally:
        gates.release()
        scheduler.close()
    assert all(runtime.service.get_operation(key)["status"] == "succeeded" for key in identifiers)


def test_receiving_and_creation_use_independent_slots_and_never_schedule_a_mailbox_twice(setup, monkeypatch):
    runtime, provider = setup
    operations = enqueue(runtime, 4)
    for _ in operations:
        runtime.service._execute_operation(runtime.service._claim_operation())
    mailboxes = [runtime.service.get_operation(item["id"])["mailbox_id"] for item in operations]
    with runtime.service.db.connect() as conn:
        upstream_ids = {row["id"]: row["upstream_id"] for row in conn.execute("SELECT id, upstream_id FROM mailboxes")}
    receive_gates = Gates(upstream_ids.values())
    pending = runtime.service.submit(
        "create", "extra-create", {"provider": "scheduler", "ttl_seconds": 3600, "required_capabilities": ["receive"]}
    )
    create_gates = Gates([pending["id"]])
    create, receive = provider.create_mailbox, provider.list_messages

    def blocked_create(ttl, request_id):
        create_gates.enter(request_id)
        return create(ttl, request_id)

    def blocked_receive(mailbox):
        receive_gates.enter(mailbox.upstream_id)
        return receive(mailbox)

    monkeypatch.setattr(provider, "create_mailbox", blocked_create)
    monkeypatch.setattr(provider, "list_messages", blocked_receive)
    scheduler = Worker(runtime)
    try:
        scheduler.tick()
        create_gates.wait(1)
        receive_gates.wait(2)
        scheduler.tick()
        assert len(scheduler.operation_jobs) == 1
        assert len(scheduler.receive_jobs) == 2
        assert len(receive_gates.entered) == 2
        first_id = next(key for key in mailboxes if upstream_ids[key] == receive_gates.entered[0])
        receive_gates.gates[upstream_ids[first_id]].set()
        finish(scheduler.receive_jobs, first_id)
        scheduler.tick()
        receive_gates.wait(3)
        assert len(scheduler.receive_jobs) == 2
        assert len(set(receive_gates.entered)) == 3
        assert runtime.service.get_operation(pending["id"])["status"] == "running"
    finally:
        receive_gates.release()
        create_gates.release()
        scheduler.close()


def test_one_tick_has_bounded_admission_and_shutdown_finishes_claimed_jobs(setup):
    runtime, _ = setup
    operations = enqueue(runtime, 6)
    scheduler = Worker(runtime)
    scheduler.tick()
    result = scheduler.close()
    assert result["operations"] == 2
    statuses = [runtime.service.get_operation(item["id"])["status"] for item in operations]
    assert statuses.count("succeeded") == 2
    assert statuses.count("pending") == 4
    scheduler.tick()
    assert [runtime.service.get_operation(item["id"])["status"] for item in operations] == statuses


def test_busy_worker_hot_reload_wakes_without_waiting_for_long_poll(setup):
    runtime, _ = setup
    runtime.prepare(replace(runtime.service.settings, worker_poll_seconds=120), "long-poll")()
    previous = runtime.service
    runtime.prepare(replace(runtime.service.settings, create_concurrency=3), "more-slots")()
    wake = Event()
    # A configuration change bypasses the old idle deadline, even if no task completed.
    wait_for_next_tick(runtime, wake, previous=previous)
    assert runtime.service.settings.create_concurrency == 3


def test_skipped_mailbox_is_not_busy_retried_on_other_task_wake(setup):
    runtime, _ = setup
    item = enqueue(runtime, 1)[0]
    runtime.service._execute_operation(runtime.service._claim_operation())
    mailbox_id = runtime.service.get_operation(item["id"])["mailbox_id"]
    scheduler = Worker(runtime)
    try:
        with runtime.service.db.mailbox_lock(mailbox_id):
            scheduler.tick()
            finish(scheduler.receive_jobs, mailbox_id)
            scheduler.tick()
            assert mailbox_id in scheduler.receive_retry_at
            assert scheduler.receive_jobs == {}
    finally:
        scheduler.close()


@pytest.mark.parametrize("strict", [False, True])
def test_background_storage_failure_propagates_only_for_strict_once_shutdown(setup, monkeypatch, strict):
    runtime, _ = setup
    item = enqueue(runtime, 1)[0]

    def fail(_operation):
        raise sqlite3.OperationalError("offline test database lock")

    monkeypatch.setattr(runtime.service, "_execute_operation", fail)
    scheduler = Worker(runtime)
    scheduler.tick()
    if strict:
        with pytest.raises(sqlite3.OperationalError, match="offline test"):
            scheduler.close(strict=True)
    else:
        assert scheduler.close()["operations"] == 0
    assert scheduler.stop.is_set()
    assert runtime.service.get_operation(item["id"])["status"] == "running"


@pytest.mark.parametrize("once", [False, True])
def test_worker_retries_storage_error_only_in_continuous_mode(monkeypatch, once):
    settings = SimpleNamespace(worker_poll_seconds=1)
    tick = Mock(side_effect=[sqlite3.OperationalError("temporary lock"), {"operations": 1, "synced": 0, "expired": 0}])
    service = SimpleNamespace(settings=settings)
    scheduler = SimpleNamespace(
        tick=tick,
        snapshot=service,
        wake=Event(),
        stop=Event(),
        request_stop=Mock(),
        close=Mock(return_value={"operations": 0, "synced": 0, "expired": 0}),
    )
    pause = Mock(side_effect=[None, KeyboardInterrupt])
    monkeypatch.setattr(worker.Settings, "from_yaml", lambda: settings)
    monkeypatch.setattr(worker, "Runtime", lambda config: SimpleNamespace(refresh=lambda: service))
    monkeypatch.setattr(worker, "Worker", lambda runtime: scheduler)
    monkeypatch.setattr(worker, "wait_for_next_tick", pause)
    monkeypatch.setattr("sys.argv", ["worker", "--once"] if once else ["worker"])
    if once:
        with pytest.raises(sqlite3.OperationalError, match="temporary lock"):
            worker.main()
        assert tick.call_count == 1
        pause.assert_not_called()
    else:
        worker.main()
        assert tick.call_count == 2
        assert pause.call_count == 2
    scheduler.close.assert_called_once()
