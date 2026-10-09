import sqlite3
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ProviderSettings, Settings
from app.providers.base import ProviderError
from app.providers.factory import build_registry
from tests.fakes import Response, ScriptedTransport
from tests.fakes import org_detail as detail
from tests.fakes import org_listing as listing


@pytest.fixture
def document():
    return {
        "app": {
            "db_path": "data/mailboxes.sqlite",
            "api_token": "yaml-api-token-with-enough-entropy",
            "encryption_key": Fernet.generate_key().decode(),
        },
        "providers": {"temp-mail-org": {"enabled": True}},
    }


def write_config(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_yaml_preserves_keys_resolves_paths_and_loads_worker_options(document, tmp_path, monkeypatch):
    document["worker"] = {"sync_interval_seconds": 23, "operation_timeout_seconds": 90, "poll_seconds": 0.5}
    config = write_config(tmp_path / "deployment" / "config.yaml", document)
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_yaml(config)
    assert settings.api_token == document["app"]["api_token"]
    assert settings.encryption_key == document["app"]["encryption_key"]
    assert Path(settings.db_path) == config.parent / "data" / "mailboxes.sqlite"
    assert settings.sync_interval_seconds == 23
    assert settings.operation_timeout_seconds == 90
    assert settings.worker_poll_seconds == 0.5
    cipher = Fernet(settings.encryption_key.encode())
    encrypted = cipher.encrypt(b"existing-upstream-credential")
    reloaded = Settings.from_yaml(config)
    assert Fernet(reloaded.encryption_key.encode()).decrypt(encrypted) == b"existing-upstream-credential"


@pytest.mark.parametrize("path_kind", ["home", "absolute"])
def test_yaml_home_and_absolute_database_paths_are_not_prefixed_by_config_directory(document, tmp_path, path_kind):
    database_path = "~/temp-mail-path-test/mailboxes.sqlite" if path_kind == "home" else str(tmp_path / "external" / "mailboxes.sqlite")
    document["app"]["db_path"] = database_path
    config = write_config(tmp_path / "deployment" / "config.yaml", document)
    settings = Settings.from_yaml(config)
    assert Path(settings.db_path) == Path(database_path).expanduser().resolve()


def test_yaml_provider_order_and_explicit_enablement_control_auto_selection(document, tmp_path, monkeypatch):
    document["providers"] = {
        "tempmail-lol": {"enabled": True},
        "temp-mail-org": {"enabled": True},
    }
    config = write_config(tmp_path / "config.yaml", document)
    settings = Settings.from_yaml(config)

    def unexpected_session(**kwargs):
        pytest.fail("Loading provider configuration must remain offline")

    monkeypatch.setattr("app.providers.temp_mail_org.requests.Session", unexpected_session)
    registry = build_registry(settings)
    assert list(settings.providers) == ["tempmail-lol", "temp-mail-org"]
    assert [provider.id for provider in registry.all()] == ["tempmail-lol", "temp-mail-org"]
    assert registry.select(["receive"], 3600).id == "tempmail-lol"
    assert registry.select(["receive"], 3601).id == "temp-mail-org"
    assert registry.get("temp-mail-org").impersonate == "chrome110"
    assert settings.sync_interval_seconds == 15
    assert settings.operation_timeout_seconds == 300
    assert settings.worker_poll_seconds == 1
    assert settings.create_concurrency == 2
    assert settings.receive_concurrency == 4
    with pytest.raises(ProviderError) as caught:
        registry.select(["send"], 3600)
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"


def test_only_explicit_yaml_providers_are_registered_and_empty_mapping_disables_all(document, tmp_path):
    document["providers"] = {"tempmail-lol": {}, "temp-mail-org": {"enabled": False}}
    config = write_config(tmp_path / "config.yaml", document)
    settings = Settings.from_yaml(config)
    assert [provider.id for provider in build_registry(settings).all()] == ["tempmail-lol"]
    document["providers"] = {}
    write_config(config, document)
    settings = Settings.from_yaml(config)
    registry = build_registry(settings)
    assert registry.all() == []
    with pytest.raises(ProviderError) as caught:
        registry.select(["receive"], 3600)
    assert caught.value.code == "PROVIDER_UNAVAILABLE"


def test_direct_settings_keep_default_providers_without_shared_mutable_options(document):
    first = Settings(**document["app"])
    second = Settings(**document["app"])
    assert list(first.providers) == ["temp-mail-org", "tempmail-lol"]
    first.providers["temp-mail-org"].options["proxy"] = "http://user:private-proxy-password@localhost:7890"
    assert "proxy" not in second.providers["temp-mail-org"].options
    assert "private-proxy-password" not in repr(first)
    assert ProviderSettings().enabled is True


def test_registry_construction_neither_creates_database_nor_adds_simulated_tables(document, tmp_path):
    document["app"]["db_path"] = str(tmp_path / "mailboxes.sqlite")
    settings = Settings(**document["app"])
    registry = build_registry(settings)
    assert registry.select(["receive"], 3600).id == "temp-mail-org"
    assert not Path(settings.db_path).exists()
    with sqlite3.connect(settings.db_path) as database:
        database.execute("CREATE TABLE existing_table (id TEXT)")
    build_registry(settings)
    with sqlite3.connect(settings.db_path) as database:
        tables = database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == [("existing_table",)]


def test_yaml_is_not_overridden_by_environment_or_dotenv(document, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEMP_MAIL_API_TOKEN", "environment-token-that-must-not-win")
    monkeypatch.setenv("TEMP_MAIL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TEMP_MAIL_DB_PATH", "environment.sqlite")
    monkeypatch.setenv("TEMP_MAIL_ORG_PROXY", "http://localhost:7890")
    (tmp_path / ".env").write_text("TEMP_MAIL_API_TOKEN=dotenv-token-that-must-not-win\nTEMP_MAIL_ORG_ENABLED=false\n")
    write_config(tmp_path / "config.yaml", document)
    settings = Settings.from_yaml()
    assert settings.api_token == document["app"]["api_token"]
    assert settings.encryption_key == document["app"]["encryption_key"]
    assert Path(settings.db_path) == tmp_path / "data" / "mailboxes.sqlite"
    assert list(settings.providers) == ["temp-mail-org"]
    assert settings.providers["temp-mail-org"].options == {}


def test_api_startup_reads_cwd_yaml_and_creates_mailbox_without_explicit_settings(document, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEMP_MAIL_API_TOKEN", "obsolete-environment-token-must-not-work")
    (tmp_path / ".env").write_text("TEMP_MAIL_ENCRYPTION_KEY=obsolete-invalid-key\n")
    write_config(tmp_path / "config.yaml", document)
    email = "private@example.test"
    transport = ScriptedTransport(Response(200, {"mailbox": email, "token": "test-mailbox-token"}), listing("mail"), detail("mail"))
    monkeypatch.setattr("app.providers.temp_mail_org.requests.Session", lambda **kwargs: transport())
    with TestClient(create_app(), headers={"Authorization": f"Bearer {document['app']['api_token']}"}) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.post("/v1/mailboxes", json={}, headers={"Idempotency-Key": "yaml-entrypoint-create"})
        assert response.status_code == 202, response.text
        client.app.state.service.run_once()
        operation = client.get(f"/v1/operations/{response.json()['id']}").json()
        assert operation["status"] == "succeeded"
        mailbox = client.get(f"/v1/mailboxes/{operation['result']['mailbox_id']}").json()
        assert mailbox["provider_id"] == "temp-mail-org"
        assert mailbox["email"] == operation["result"]["email"] == email
        messages = client.get(f"/v1/mailboxes/{mailbox['id']}/messages").json()["items"]
        assert len(messages) == 1
        received = client.get(f"/v1/mailboxes/{mailbox['id']}/messages/{messages[0]['id']}").json()
        assert received["text"] == "Hello"
        assert client.app.state.service.settings.encryption_key == document["app"]["encryption_key"]
        assert Path(client.app.state.service.db.path) == tmp_path / "data" / "mailboxes.sqlite"


def test_missing_yaml_does_not_fall_back_to_dotenv_or_environment(document, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEMP_MAIL_API_TOKEN", document["app"]["api_token"])
    monkeypatch.setenv("TEMP_MAIL_ENCRYPTION_KEY", document["app"]["encryption_key"])
    (tmp_path / ".env").write_text("TEMP_MAIL_DB_PATH=data/from-dotenv.sqlite\n")
    with pytest.raises(FileNotFoundError):
        Settings.from_yaml()


@pytest.mark.parametrize("section", ["app", "providers"])
def test_required_yaml_sections_cannot_be_omitted(document, tmp_path, section):
    document.pop(section)
    with pytest.raises(ValueError):
        Settings.from_yaml(write_config(tmp_path / "config.yaml", document))


@pytest.mark.parametrize("location", ["top", "app", "worker", "provider_id", "provider_option"])
def test_unknown_configuration_fields_are_rejected_without_echoing_values(document, tmp_path, location):
    secret = "unknown-field-value-must-remain-private"
    if location == "top":
        document["unknown"] = secret
    elif location == "app":
        document["app"]["unknown"] = secret
    elif location == "worker":
        document["worker"] = {"unknown": secret}
    elif location == "provider_id":
        document["providers"] = {"unsupported-provider": {"enabled": True, "api_key": secret}}
    else:
        document["providers"] = {"temp-mail-org": {"unknown": secret}}
    with pytest.raises(ValueError) as caught:
        settings = Settings.from_yaml(write_config(tmp_path / "config.yaml", document))
        build_registry(settings)
    assert secret not in str(caught.value)


@pytest.mark.parametrize("enabled", ["false", 0, None])
def test_provider_enabled_requires_actual_boolean(document, tmp_path, enabled):
    document["providers"]["temp-mail-org"]["enabled"] = enabled
    with pytest.raises(ValueError):
        settings = Settings.from_yaml(write_config(tmp_path / "config.yaml", document))
        build_registry(settings)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("worker", "poll_seconds", float("nan")),
        ("worker", "sync_interval_seconds", True),
        ("worker", "operation_timeout_seconds", 0),
        ("provider", "timeout_seconds", float("inf")),
        ("provider", "timeout_seconds", -1),
    ],
)
def test_invalid_numeric_configuration_is_rejected(document, tmp_path, section, field, value):
    if section == "worker":
        document["worker"] = {field: value}
    else:
        document["providers"] = {"temp-mail-org": {field: value}}
    with pytest.raises(ValueError):
        settings = Settings.from_yaml(write_config(tmp_path / "config.yaml", document))
        build_registry(settings)


@pytest.mark.parametrize("field", ["create_concurrency", "receive_concurrency"])
@pytest.mark.parametrize("value", [0, -1, 33, True, 2.0, "2", None])
def test_worker_concurrency_requires_bounded_integer(document, tmp_path, field, value):
    document["worker"] = {field: value}
    with pytest.raises(ValueError, match=field):
        Settings.from_yaml(write_config(tmp_path / "config.yaml", document))
    with pytest.raises(ValueError, match=field):
        Settings(**document["app"], **{field: value})


@pytest.mark.parametrize("value", [1, 32])
def test_worker_concurrency_accepts_bounds(document, tmp_path, value):
    document["worker"] = {"create_concurrency": value, "receive_concurrency": value}
    settings = Settings.from_yaml(write_config(tmp_path / "config.yaml", document))
    assert settings.create_concurrency == settings.receive_concurrency == value


def test_invalid_yaml_syntax_does_not_expose_secret_source_lines(tmp_path):
    secret = "private-yaml-api-token-never-echo-this"
    config = tmp_path / "config.yaml"
    config.write_text(f'app:\n  api_token: "{secret}\nproviders: {{}}\n')
    with pytest.raises(ValueError) as caught:
        Settings.from_yaml(config)
    assert secret not in str(caught.value)


def test_duplicate_yaml_keys_are_rejected_without_exposing_secret_values(document, tmp_path):
    secret = "second-private-api-token-never-echo-this"
    config = write_config(tmp_path / "config.yaml", document)
    config.write_text(config.read_text().replace("app:\n", f"app:\n  api_token: {secret}\n", 1))
    with pytest.raises(ValueError) as caught:
        Settings.from_yaml(config)
    assert secret not in str(caught.value)


def test_invalid_provider_proxy_option_is_rejected_without_exposing_credentials(document, tmp_path):
    document["providers"] = {"temp-mail-org": {"proxy": "http://username:private-proxy-secret@localhost:invalid"}}
    with pytest.raises(ValueError) as caught:
        settings = Settings.from_yaml(write_config(tmp_path / "config.yaml", document))
        build_registry(settings)
    assert "private-proxy-secret" not in str(caught.value)
