import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.config_store import ConfigStore
from app.errors import ServiceError


@pytest.fixture
def document():
    return {
        "app": {
            "db_path": "data/mail.sqlite",
            "api_token": "test-configuration-token-with-enough-entropy",
            "encryption_key": Fernet.generate_key().decode(),
        },
        "worker": {"sync_interval_seconds": 23, "poll_seconds": 0.5},
        "providers": {
            "tempmail-lol": {"enabled": False, "proxy": "http://name:private-proxy-secret@localhost:8080"},
        },
    }


@pytest.fixture
def config_file(tmp_path, document):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


@pytest.fixture
def client(config_file):
    settings = Settings.from_yaml(config_file)
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        yield client


def patch(client, **changes):
    return {"revision": client.get("/v1/config").json()["revision"], **changes}


def providers(client):
    return [
        {key: value for key, value in item.items() if key not in {"proxy_configured", "capabilities"}}
        for item in client.get("/v1/config").json()["providers"]
    ]


def test_config_get_is_authenticated_and_never_returns_credentials(client, document):
    unauthorized = client.get("/v1/config", headers={"Authorization": "Bearer wrong"})
    assert unauthorized.status_code == 401
    assert unauthorized.headers["cache-control"] == "no-store"
    response = client.get("/v1/config")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["restart_required"] is False
    assert body["app"] == {"db_path": "data/mail.sqlite", "api_token_configured": True, "encryption_key_configured": True}
    assert body["worker"] == {
        "sync_interval_seconds": 23,
        "operation_timeout_seconds": 300,
        "poll_seconds": 0.5,
        "create_concurrency": 2,
        "receive_concurrency": 4,
    }
    assert [item["id"] for item in body["providers"]] == ["tempmail-lol", "temp-mail-org"]
    assert [item["enabled"] for item in body["providers"]] == [False, False]
    assert [item["proxy_configured"] for item in body["providers"]] == [True, False]
    assert body["providers"][1]["impersonate"] == "chrome110"
    for secret in (document["app"]["api_token"], document["app"]["encryption_key"], "private-proxy-secret"):
        assert secret not in response.text


def test_save_persists_sections_and_applies_new_token_immediately(client, config_file, document):
    new_token = "new-test-token-that-takes-effect-immediately"
    response = client.put("/v1/config", json=patch(client, app={"api_token": new_token}, worker={"poll_seconds": 2.5}))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["restart_required"] is False
    assert new_token not in response.text
    assert client.get("/v1/config").status_code == 401
    assert client.get("/v1/config", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200
    assert client.app.state.service.settings.worker_poll_seconds == 2.5
    assert yaml.safe_load(config_file.read_text())["providers"] == document["providers"]
    settings = Settings.from_yaml(config_file)
    assert settings.worker_poll_seconds == 2.5
    assert settings.encryption_key == document["app"]["encryption_key"]
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {new_token}"}) as restarted:
        assert restarted.get("/v1/config").json()["restart_required"] is False
        assert restarted.get("/v1/config", headers={"Authorization": f"Bearer {document['app']['api_token']}"}).status_code == 401


def test_concurrency_save_persists_and_updates_runtime_without_restart(client, config_file):
    response = client.put("/v1/config", json=patch(client, worker={"create_concurrency": 1, "receive_concurrency": 32}))
    assert response.status_code == 200
    assert response.json()["restart_required_fields"] == []
    assert response.json()["restart_required"] is False
    assert client.app.state.service.settings.create_concurrency == 1
    assert client.app.state.service.settings.receive_concurrency == 32
    saved = Settings.from_yaml(config_file)
    assert saved.create_concurrency == 1
    assert saved.receive_concurrency == 32
    with TestClient(create_app(saved), headers={"Authorization": f"Bearer {saved.api_token}"}) as restarted:
        current = restarted.get("/v1/config").json()
        assert current["worker"]["create_concurrency"] == 1
        assert current["worker"]["receive_concurrency"] == 32
        assert current["restart_required"] is False


@pytest.mark.parametrize("field", ["create_concurrency", "receive_concurrency"])
@pytest.mark.parametrize("value", [0, -1, 33, True, 2.0, "2", None])
def test_invalid_concurrency_save_preserves_config_and_runtime(client, config_file, field, value):
    before = config_file.read_bytes()
    original = client.app.state.service
    response = client.put("/v1/config", json=patch(client, worker={field: value}))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert config_file.read_bytes() == before
    assert client.app.state.service is original


def test_database_path_save_does_not_create_or_migrate_database(client, config_file):
    original_db = client.app.state.service.settings.db_path
    response = client.put("/v1/config", json=patch(client, app={"db_path": "other/new.sqlite"}))
    assert response.status_code == 200
    assert response.json()["app"]["db_path"] == "other/new.sqlite"
    assert response.json()["restart_required"] is True
    assert response.json()["restart_required_fields"] == ["app.db_path"]
    assert client.app.state.service.settings.db_path == original_db
    assert Path(original_db).is_file()
    assert not (config_file.parent / "other").exists()
    assert Settings.from_yaml(config_file).db_path == str(config_file.parent / "other/new.sqlite")


@pytest.mark.parametrize("proxy_value", ["omitted", "", "   "])
def test_reordering_providers_preserves_secrets_and_other_sections(client, config_file, document, proxy_value):
    items = providers(client)[::-1]
    if proxy_value != "omitted":
        items[1]["proxy"] = proxy_value
    items[0]["enabled"] = True
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 200
    saved = yaml.safe_load(config_file.read_text())
    assert list(saved["providers"]) == ["temp-mail-org", "tempmail-lol"]
    assert saved["providers"]["tempmail-lol"]["proxy"] == document["providers"]["tempmail-lol"]["proxy"]
    assert saved["app"] == document["app"]
    assert saved["worker"] == document["worker"]
    assert [item["id"] for item in response.json()["providers"]] == list(saved["providers"])
    assert [provider.id for provider in client.app.state.service.registry.all()] == ["temp-mail-org"]


def test_proxy_can_be_replaced_then_explicitly_cleared(client, config_file):
    items = providers(client)
    items[0]["proxy"] = "socks5h://next:changed-private-secret@localhost:7890"
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 200
    assert "changed-private-secret" not in response.text
    assert response.json()["providers"][0]["proxy_configured"] is True
    items[0]["proxy"] = None
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 200
    assert response.json()["providers"][0]["proxy_configured"] is False
    assert Settings.from_yaml(config_file).providers["tempmail-lol"].options["proxy"] is None


def test_empty_api_token_preserves_existing_token(client, config_file, document):
    response = client.put("/v1/config", json=patch(client, app={"api_token": ""}))
    assert response.status_code == 200
    assert response.json()["restart_required"] is False
    assert Settings.from_yaml(config_file).api_token == document["app"]["api_token"]


@pytest.mark.parametrize(
    "changes",
    [
        {"unexpected": "private-invalid-value"},
        {"app": {"encryption_key": "private-invalid-value"}},
        {"app": {"api_token": "short"}},
        {"app": {"db_path": ":memory:"}},
        {"app": {"db_path": " "}},
        {"app": {"db_path": None}},
        {"app": None},
        {"worker": {"poll_seconds": 0}},
        {"worker": {"poll_seconds": None}},
        {"worker": {"sync_interval_seconds": True}},
        {"worker": {"operation_timeout_seconds": 1.5}},
        {"worker": {"poll_seconds": "2"}},
        {"providers": None},
    ],
)
def test_invalid_changes_leave_file_untouched_and_redact_values(client, config_file, changes):
    before = config_file.read_bytes()
    response = client.put("/v1/config", json=patch(client, **changes))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert "private-invalid-value" not in response.text
    assert config_file.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "unknown-provider"),
        ("enabled", "false"),
        ("base_url", "ftp://invalid.example"),
        ("base_url", "http://example.test:invalid"),
        ("base_url", "http://user:private-invalid-value@example.test"),
        ("index_url", "javascript:alert(1)"),
        ("impersonate", "chrome-does-not-exist"),
        ("timeout_seconds", -1),
        ("proxy", "http://user:private-invalid-value@localhost:invalid"),
        ("extra", "private-invalid-value"),
    ],
)
def test_invalid_provider_fields_rejected_offline(client, config_file, field, value):
    items = providers(client)
    items[0][field] = value
    before = config_file.read_bytes()
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert "private-invalid-value" not in response.text
    assert config_file.read_bytes() == before


def test_duplicate_provider_ids_are_rejected(client, config_file):
    item = providers(client)[0]
    before = config_file.read_bytes()
    response = client.put("/v1/config", json=patch(client, providers=[item, item]))
    assert response.status_code == 422
    assert config_file.read_bytes() == before


def test_stale_revision_does_not_overwrite_other_editor(client, config_file):
    stale = patch(client, worker={"poll_seconds": 9})
    response = client.put("/v1/config", json=patch(client, worker={"poll_seconds": 2}))
    assert response.status_code == 200
    before = config_file.read_bytes()
    conflict = client.put("/v1/config", json=stale)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFIG_CONFLICT"
    assert config_file.read_bytes() == before


def test_external_file_changes_are_visible_and_conflict_with_old_form(client, config_file):
    stale = patch(client, worker={"poll_seconds": 9})
    saved = yaml.safe_load(config_file.read_text())
    saved["worker"]["poll_seconds"] = 6
    config_file.write_text(yaml.safe_dump(saved, sort_keys=False))
    assert client.get("/v1/config").json()["worker"]["poll_seconds"] == 6
    assert client.get("/v1/config").json()["restart_required"] is False
    assert client.app.state.service.settings.worker_poll_seconds == 6
    assert client.put("/v1/config", json=stale).status_code == 409


@pytest.mark.parametrize("failure", ["replace", "fsync", "lock"])
def test_atomic_write_failure_keeps_original_and_removes_temporary_file(client, config_file, monkeypatch, failure):
    update = patch(client, worker={"poll_seconds": 8})
    before = config_file.read_bytes()

    def denied(*args, **kwargs):
        raise PermissionError("private-filesystem-error-path")

    monkeypatch.setattr(f"app.config_store.os.{'open' if failure == 'lock' else failure}", denied)
    response = client.put("/v1/config", json=update)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONFIG_WRITE_FAILED"
    assert "private-filesystem-error-path" not in response.text
    assert config_file.read_bytes() == before
    assert not list(config_file.parent.glob(".config.yaml.*.tmp"))


def test_atomic_save_uses_private_permissions(client, config_file):
    config_file.chmod(0o644)
    response = client.put("/v1/config", json=patch(client, worker={"poll_seconds": 8}))
    assert response.status_code == 200
    if os.name != "nt":
        assert config_file.stat().st_mode & 0o777 == 0o600
    assert not list(config_file.parent.glob(".config.yaml.*.tmp"))


@pytest.mark.parametrize("broken", ["missing", "invalid"])
def test_read_errors_are_generic_and_do_not_overwrite_broken_file(client, config_file, broken):
    update = patch(client, worker={"poll_seconds": 2})
    if broken == "missing":
        config_file.unlink()
    else:
        config_file.write_text('app:\n  api_token: "private-parser-secret\nproviders: {}\n')
    for response in (client.get("/v1/config"), client.put("/v1/config", json=update)):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CONFIG_READ_FAILED"
        assert "private-parser-secret" not in response.text
    assert config_file.exists() is (broken != "missing")


def test_direct_settings_without_file_keep_business_api_available(tmp_path, document):
    settings = Settings(**{**document["app"], "db_path": str(tmp_path / "direct.sqlite")}, providers={})
    with TestClient(create_app(settings), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.get("/v1/config")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CONFIG_UNAVAILABLE"
        response = client.put("/v1/config", json={"revision": "0" * 64})
        assert response.json()["error"]["code"] == "CONFIG_UNAVAILABLE"


def test_separate_stores_serialize_concurrent_writers(config_file):
    settings = Settings.from_yaml(config_file)
    stores = [ConfigStore(settings), ConfigStore(settings)]
    revision = stores[0].get()["revision"]

    def save(index):
        try:
            return stores[index].save({"revision": revision, "worker": {"poll_seconds": index + 2}})
        except ServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("CONFIG_CONFLICT") == 1
    assert Settings.from_yaml(config_file).worker_poll_seconds in {2, 3}


def test_file_selector_environment_does_not_override_yaml_values(config_file, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv("TEMP_MAIL_CONFIG", str(config_file))
    settings = Settings.from_yaml()
    assert settings.config_path == config_file
    assert settings.db_path == str(config_file.parent / "data/mail.sqlite")
    assert settings == Settings.from_yaml(config_file)
    assert "config_path" not in repr(settings)


def test_provider_only_order_change_is_applied_without_restart(client, config_file):
    initial = providers(client)
    # Save the same default-expanded provider configuration first.
    response = client.put("/v1/config", json=patch(client, providers=initial))
    assert response.status_code == 200
    assert response.json()["restart_required"] is False
    response = client.put("/v1/config", json=patch(client, providers=initial[::-1]))
    assert response.status_code == 200
    assert response.json()["restart_required"] is False


def test_provider_validation_does_not_open_network_sessions(client, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Configuration editing must not make provider requests")

    monkeypatch.setattr("app.providers.temp_mail_org.requests.Session", unexpected)
    items = providers(client)
    for item in items:
        item["enabled"] = True
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 200


def test_read_only_configuration_rejects_save_without_replacement(client, config_file):
    update = patch(client, worker={"poll_seconds": 8})
    before = config_file.read_bytes()
    config_file.chmod(0o444)
    try:
        response = client.put("/v1/config", json=update)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "CONFIG_WRITE_FAILED"
        assert config_file.read_bytes() == before
    finally:
        config_file.chmod(0o600)


@pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX ownership")
def test_save_preserves_file_owner_and_group(client, config_file):
    previous = config_file.stat()
    response = client.put("/v1/config", json=patch(client, worker={"poll_seconds": 8}))
    assert response.status_code == 200
    current = config_file.stat()
    assert (current.st_uid, current.st_gid) == (previous.st_uid, previous.st_gid)


@pytest.mark.skipif(not hasattr(os, "fchown"), reason="POSIX ownership")
def test_save_applies_original_ownership_when_writer_owns_temp_file(client, config_file, monkeypatch):
    from types import SimpleNamespace

    owner = config_file.stat()
    changed_ownership = []
    monkeypatch.setattr("app.config_store.os.fstat", lambda fd: SimpleNamespace(st_uid=owner.st_uid + 1, st_gid=owner.st_gid + 1))
    monkeypatch.setattr("app.config_store.os.fchown", lambda fd, uid, gid: changed_ownership.append((uid, gid)))
    response = client.put("/v1/config", json=patch(client, worker={"poll_seconds": 8}))
    assert response.status_code == 200
    assert changed_ownership == [(owner.st_uid, owner.st_gid), (owner.st_uid, owner.st_gid)]


def test_empty_informational_index_url_uses_homepage_default(client):
    items = providers(client)
    items[0]["index_url"] = ""
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 200
    assert response.json()["providers"][0]["index_url"] == "https://tempmail.lol/"


@pytest.mark.parametrize("body", [[], "secret-invalid-request", None])
def test_nonobject_save_body_has_generic_config_error(client, body):
    response = client.put("/v1/config", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert response.headers["cache-control"] == "no-store"
    assert "secret-invalid-request" not in response.text


def test_disabled_and_missing_providers_expose_configured_capabilities(client):
    response = client.get("/v1/config")
    assert response.status_code == 200
    for provider, expected in zip(response.json()["providers"], [3600, 86400], strict=True):
        assert provider["enabled"] is False
        assert provider["max_ttl_seconds"] == expected
        assert provider["capabilities"]["max_ttl_seconds"] == expected
        assert provider["capabilities"]["receive"] is True
        assert provider["capabilities"]["send"] is False


def test_ttl_save_applies_capabilities_immediately_and_legacy_save_preserves_value(client, config_file):
    items = providers(client)
    items[0]["enabled"] = True
    items[0]["max_ttl_seconds"] = 172800
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 200
    assert response.json()["restart_required"] is False
    assert response.json()["providers"][0]["capabilities"]["max_ttl_seconds"] == 172800
    assert client.get("/v1/capabilities").json()["providers"][0]["capabilities"]["max_ttl_seconds"] == 172800
    for item in items:
        item.pop("max_ttl_seconds")
    items[0]["timeout_seconds"] = 30
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 200
    assert response.json()["providers"][0]["max_ttl_seconds"] == 172800
    restarted_settings = Settings.from_yaml(config_file)
    with TestClient(create_app(restarted_settings), headers=client.headers) as restarted:
        assert restarted.get("/v1/config").json()["restart_required"] is False
        assert restarted.get("/v1/capabilities").json()["providers"][0]["capabilities"]["max_ttl_seconds"] == 172800
        allowed = restarted.post("/v1/mailboxes", json={"ttl_seconds": 172800}, headers={"Idempotency-Key": "ttl-allowed"})
        assert allowed.status_code == 202
        rejected = restarted.post("/v1/mailboxes", json={"ttl_seconds": 172801}, headers={"Idempotency-Key": "ttl-rejected"})
        assert rejected.status_code == 422
        assert rejected.json()["error"]["code"] == "TTL_UNSUPPORTED"


@pytest.mark.parametrize("value", [None, False, 59, 31536001, 3600.5, "3600"])
def test_invalid_provider_max_ttl_leaves_yaml_unchanged(client, config_file, value):
    items = providers(client)
    items[0]["max_ttl_seconds"] = value
    before = config_file.read_bytes()
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert config_file.read_bytes() == before


def test_provider_capabilities_are_readonly(client, config_file):
    items = providers(client)
    items[0]["capabilities"] = {"send": True}
    before = config_file.read_bytes()
    response = client.put("/v1/config", json=patch(client, providers=items))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert config_file.read_bytes() == before
