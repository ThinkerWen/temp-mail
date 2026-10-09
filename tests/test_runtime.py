import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import worker
from app.api import create_app
from app.config import Settings
from app.config_store import ConfigStore, _revision
from app.errors import ServiceError
from app.providers import TempMailOrgProvider
from app.providers.base import ProviderMailbox
from app.providers.registry import Registry
from app.runtime import Runtime
from app.service import Service


@pytest.fixture
def document():
    return {
        "app": {
            "db_path": "data/original.sqlite",
            "api_token": "original-runtime-token-with-enough-entropy",
            "encryption_key": Fernet.generate_key().decode(),
        },
        "worker": {"poll_seconds": 0.5, "sync_interval_seconds": 15},
        "providers": {"temp-mail-org": {"base_url": "https://original.example.test", "max_ttl_seconds": 600}},
    }


@pytest.fixture
def config_file(tmp_path, document):
    path = tmp_path / "config.yaml"
    write(path, document)
    return path


def write(path, document):
    path.write_text(yaml.safe_dump(document, sort_keys=False))


@pytest.fixture
def runtime(config_file):
    runtime = Runtime(Settings.from_yaml(config_file))
    runtime.refresh()
    return runtime


def test_hot_update_reuses_database_and_cipher_and_preserves_old_snapshot(runtime, config_file, document, monkeypatch):
    original = runtime.service
    monkeypatch.setattr("app.db.Database.initialize", lambda *args: pytest.fail("Reload must reuse initialized storage"))
    document["worker"] = {
        "poll_seconds": 2,
        "sync_interval_seconds": 60,
        "operation_timeout_seconds": 123,
        "create_concurrency": 7,
        "receive_concurrency": 9,
    }
    document["providers"] = {
        "tempmail-lol": {"max_ttl_seconds": 7200},
        "temp-mail-org": {
            "base_url": "https://changed.example.test",
            "max_ttl_seconds": 172800,
            "timeout_seconds": 9,
            "impersonate": "chrome",
            "proxy": "http://private:test-credential@localhost:8080",
        },
    }
    write(config_file, document)
    updated = runtime.refresh()
    assert updated is not original
    assert updated.db is original.db
    assert updated.cipher is original.cipher
    assert original.settings.worker_poll_seconds == 0.5
    assert original.settings.create_concurrency == 2
    assert original.settings.receive_concurrency == 4
    assert original.registry.get("temp-mail-org").base_url == "https://original.example.test"
    assert updated.settings.worker_poll_seconds == 2
    assert updated.settings.sync_interval_seconds == 60
    assert updated.settings.operation_timeout_seconds == 123
    assert updated.settings.create_concurrency == 7
    assert updated.settings.receive_concurrency == 9
    assert [provider.id for provider in updated.registry.all()] == ["tempmail-lol", "temp-mail-org"]
    configured = updated.registry.get("temp-mail-org")
    assert configured.base_url == "https://changed.example.test"
    assert configured.capabilities.max_ttl_seconds == 172800
    assert configured.timeout_seconds == 9
    assert configured.impersonate == "chrome"
    assert configured.proxy == document["providers"]["temp-mail-org"]["proxy"]
    assert runtime.configuration()["restart_required_fields"] == []


def test_only_cold_fields_remain_at_startup_values(runtime, config_file, document):
    original = runtime.service
    encrypted = original.cipher.encrypt(b"private-mailbox-credential")
    document["app"]["db_path"] = "new/location.sqlite"
    document["app"]["encryption_key"] = Fernet.generate_key().decode()
    document["worker"]["poll_seconds"] = 3
    write(config_file, document)
    updated = runtime.refresh()
    assert updated.settings.db_path == original.settings.db_path
    assert updated.settings.encryption_key == original.settings.encryption_key
    assert updated.settings.worker_poll_seconds == 3
    assert updated.cipher.decrypt(encrypted) == b"private-mailbox-credential"
    assert not (config_file.parent / "new").exists()
    view = runtime.configuration()
    assert view["restart_required"] is True
    assert view["restart_required_fields"] == ["app.db_path", "app.encryption_key"]
    assert ConfigStore(Settings.from_yaml(config_file)).get()["restart_required_fields"] == []


def test_same_revision_does_not_parse_yaml_or_rebuild_providers(runtime, monkeypatch):
    original = runtime.service
    read = Mock(side_effect=AssertionError("An unchanged file should not be parsed again"))
    monkeypatch.setattr(runtime.config_store, "_read", read)
    assert runtime.refresh() is original
    assert runtime.refresh() is original
    read.assert_not_called()


def test_invalid_file_is_cached_redacted_and_recovers(runtime, config_file, document, monkeypatch):
    original = runtime.service
    warning = Mock()
    monkeypatch.setattr("app.runtime.logger.warning", warning)
    config_file.write_text('app:\n  api_token: "private-invalid-secret\n')
    read = Mock(wraps=runtime.config_store._read)
    monkeypatch.setattr(runtime.config_store, "_read", read)
    assert runtime.refresh() is original
    assert runtime.refresh() is original
    assert read.call_count == 1
    assert warning.call_count == 1
    assert "private-invalid-secret" not in str(warning.call_args)
    document["worker"]["poll_seconds"] = 4
    write(config_file, document)
    assert runtime.refresh().settings.worker_poll_seconds == 4
    assert read.call_count == 2


def test_missing_file_keeps_last_runtime_until_restored(runtime, config_file, document):
    original = runtime.service
    config_file.unlink()
    assert runtime.refresh() is original
    assert runtime.refresh() is original
    write(config_file, document)
    assert runtime.refresh() is original
    document["worker"]["poll_seconds"] = 8
    write(config_file, document)
    assert runtime.refresh().settings.worker_poll_seconds == 8


@pytest.mark.parametrize("failure", ["prepare", "replace"])
def test_failed_save_changes_neither_file_nor_runtime(runtime, config_file, monkeypatch, failure):
    original = runtime.service
    content = config_file.read_bytes()

    def fail(*args, **kwargs):
        if failure == "prepare":
            raise ValueError("private-candidate-error")
        raise PermissionError("private-write-error")

    if failure == "prepare":
        monkeypatch.setattr(runtime.config_store, "_prepare", fail)
    else:
        monkeypatch.setattr(runtime.config_store, "_replace", fail)
    with pytest.raises(ServiceError) as caught:
        runtime.config_store.save({"revision": _revision(content), "worker": {"poll_seconds": 4}})
    assert caught.value.code == ("CONFIG_INVALID" if failure == "prepare" else "CONFIG_WRITE_FAILED")
    assert "private" not in caught.value.message
    assert config_file.read_bytes() == content
    assert runtime.service is original


def test_stale_prepared_candidate_cannot_replace_newer_external_configuration(runtime, config_file, document):
    candidate_document = copy.deepcopy(document)
    candidate_document["worker"]["poll_seconds"] = 2
    candidate_settings = Settings.from_mapping(candidate_document, config_file)
    candidate_bytes = yaml.safe_dump(candidate_document, sort_keys=False).encode()
    commit = runtime.prepare(candidate_settings, _revision(candidate_bytes))
    document["worker"]["poll_seconds"] = 3
    write(config_file, document)
    latest = runtime.refresh()
    commit()
    assert runtime.service is latest
    assert runtime.service.settings.worker_poll_seconds == 3


def test_injected_registry_survives_hot_configuration_changes(config_file, document):
    registry = Registry([])
    runtime = Runtime(Settings.from_yaml(config_file), registry)
    document["worker"]["poll_seconds"] = 7
    write(config_file, document)
    assert runtime.refresh().registry is registry
    assert runtime.service.settings.worker_poll_seconds == 7


def test_request_authentication_and_business_logic_use_one_snapshot(config_file, document, monkeypatch):
    settings = Settings.from_yaml(config_file)
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        original = client.app.state.runtime.service
        alternate = Service(
            replace(settings, api_token="other-token-that-is-not-the-request-token"), Registry([]), db=original.db, cipher=original.cipher
        )
        refresh = Mock(side_effect=[original, alternate])
        monkeypatch.setattr(client.app.state.runtime, "refresh", refresh)
        response = client.get("/v1/capabilities")
        assert response.status_code == 200
        assert response.json()["providers"][0]["id"] == "temp-mail-org"
        assert refresh.call_count == 1


def test_api_external_token_change_takes_effect_without_restart(config_file, document):
    settings = Settings.from_yaml(config_file)
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        document["app"]["api_token"] = "replacement-token-with-enough-entropy"
        write(config_file, document)
        assert client.get("/v1/capabilities").status_code == 401
        response = client.get("/v1/capabilities", headers={"Authorization": f"Bearer {document['app']['api_token']}"})
        assert response.status_code == 200


def test_worker_shortens_long_poll_after_one_second(runtime, config_file, document, monkeypatch):
    document["worker"]["poll_seconds"] = 120
    write(config_file, document)
    runtime.refresh()
    now = [0.0]
    pauses = []
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])

    def pause(delay):
        pauses.append(delay)
        now[0] += delay
        document["worker"]["poll_seconds"] = 0.1
        write(config_file, document)

    monkeypatch.setattr(worker.time, "sleep", pause)
    worker.wait_for_next_tick(runtime)
    assert pauses == [1]
    assert runtime.service.settings.worker_poll_seconds == 0.1


def test_worker_extends_poll_without_busy_loop(monkeypatch):
    now = [0.0]
    pauses = []
    service = SimpleNamespace(settings=SimpleNamespace(worker_poll_seconds=0.1))
    runtime = SimpleNamespace(refresh=lambda: service)
    monkeypatch.setattr(worker.time, "monotonic", lambda: now[0])

    def pause(delay):
        pauses.append(delay)
        now[0] += delay
        service.settings.worker_poll_seconds = 2.5

    monkeypatch.setattr(worker.time, "sleep", pause)
    worker.wait_for_next_tick(runtime)
    assert sum(pauses) == pytest.approx(2.5)
    assert all(0 < delay <= 1 for delay in pauses)


def test_worker_adopts_provider_saved_after_tick_started_before_executing_claim(runtime, config_file, document, monkeypatch):
    old = runtime.service
    document["providers"] = {}
    write(config_file, document)
    old = runtime.refresh()
    calls = []

    def configure_and_enqueue():
        document["providers"] = {"temp-mail-org": {"base_url": "https://new.example.test", "max_ttl_seconds": 600}}
        write(config_file, document)
        latest = runtime.refresh()
        latest.submit(
            "create", "new-provider-operation", {"provider": "temp-mail-org", "ttl_seconds": 600, "required_capabilities": ["receive"]}
        )
        return 0

    def create(provider, ttl, request_id):
        calls.append(provider.base_url)
        return ProviderMailbox("mock-upstream", "safe@example.test", "mock-token")

    monkeypatch.setattr(old, "expire_mailboxes", configure_and_enqueue)
    monkeypatch.setattr(TempMailOrgProvider, "create_mailbox", create)
    monkeypatch.setattr(TempMailOrgProvider, "list_messages", lambda *args: [])
    result = old.run_once(refresh=runtime.refresh)
    assert result["operations"] == 1
    assert calls == ["https://new.example.test"]
    with old.db.connect() as conn:
        assert conn.execute("SELECT status FROM operations").fetchone()[0] == "succeeded"


def test_inflight_worker_operation_keeps_snapshot_and_is_not_replayed(runtime, config_file, document, monkeypatch):
    old = runtime.service
    entered = Event()
    release = Event()
    calls = []

    def create(provider, ttl, request_id):
        calls.append(provider.base_url)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            assert provider.base_url == "https://original.example.test"
        return ProviderMailbox(f"upstream-{len(calls)}", f"safe-{len(calls)}@example.test", "mock-token")

    monkeypatch.setattr(TempMailOrgProvider, "create_mailbox", create)
    monkeypatch.setattr(TempMailOrgProvider, "list_messages", lambda *args: [])
    payload = {"provider": "temp-mail-org", "ttl_seconds": 600, "required_capabilities": ["receive"]}
    first = old.submit("create", "first-operation", payload)
    with ThreadPoolExecutor(max_workers=1) as executor:
        tick = executor.submit(old.run_once, runtime.refresh)
        try:
            assert entered.wait(5)
            document["providers"]["temp-mail-org"]["base_url"] = "https://changed.example.test"
            write(config_file, document)
            latest = runtime.refresh()
            assert latest is not old
        finally:
            release.set()
        assert tick.result(timeout=5)["operations"] == 1
    assert latest.get_operation(first["id"])["status"] == "succeeded"
    second = latest.submit("create", "second-operation", payload)
    latest.run_once(refresh=runtime.refresh)
    assert latest.get_operation(second["id"])["status"] == "succeeded"
    assert calls == ["https://original.example.test", "https://changed.example.test"]
