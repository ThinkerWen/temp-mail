from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ProviderSettings, Settings
from app.providers import TempMailLolProvider, TempMailOrgProvider
from app.providers.base import ProviderError, ProviderMailbox
from app.providers.factory import build_registry
from app.providers.registry import Registry
from tests.fakes import Response, ScriptedTransport
from tests.fakes import org_listing as listing


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "ttl.sqlite"),
        api_token="provider-ttl-test-token-with-enough-entropy",
        encryption_key=Fernet.generate_key().decode(),
    )


@pytest.mark.parametrize(("provider_class", "default"), [(TempMailOrgProvider, 86400), (TempMailLolProvider, 3600)])
def test_max_ttl_capabilities_are_isolated_per_instance(provider_class, default):
    original_capabilities = provider_class.capabilities
    changed = provider_class(max_ttl_seconds=172800)
    unchanged = provider_class()
    assert changed.capabilities.max_ttl_seconds == 172800
    assert unchanged.capabilities.max_ttl_seconds == default
    assert provider_class.capabilities is original_capabilities
    assert original_capabilities.max_ttl_seconds == default
    assert changed.capabilities.receive == original_capabilities.receive
    assert changed.capabilities.destructive_receive == original_capabilities.destructive_receive


@pytest.mark.parametrize("value", [None, True, 59, 31536001, 3600.0, "3600"])
@pytest.mark.parametrize("provider_class", [TempMailOrgProvider, TempMailLolProvider])
def test_invalid_ttl_limits_are_rejected_in_settings_and_adapter(settings, provider_class, value):
    with pytest.raises(ValueError):
        provider_class(max_ttl_seconds=value)
    with pytest.raises(ValueError):
        Settings(
            db_path=settings.db_path,
            api_token=settings.api_token,
            encryption_key=settings.encryption_key,
            providers={provider_class.id: ProviderSettings(enabled=False, options={"max_ttl_seconds": value})},
        )


@pytest.mark.parametrize("provider_class", [TempMailOrgProvider, TempMailLolProvider])
def test_adapter_rejects_above_configured_limit_without_network(provider_class):
    def no_network():
        pytest.fail("Rejected mailbox creation must not call the provider")

    provider = provider_class(max_ttl_seconds=600, session_factory=no_network)
    with pytest.raises(ProviderError) as error:
        provider.create_mailbox(601, "rejected")
    assert error.value.code == "TTL_UNSUPPORTED"


def test_registry_uses_each_providers_configured_local_lifetime(settings):
    configured = Settings(
        db_path=settings.db_path,
        api_token=settings.api_token,
        encryption_key=settings.encryption_key,
        providers={
            "temp-mail-org": ProviderSettings(options={"max_ttl_seconds": 600}),
            "tempmail-lol": ProviderSettings(options={"max_ttl_seconds": 172800}),
        },
    )
    registry = build_registry(configured)
    assert registry.select(["receive"], 600).id == "temp-mail-org"
    assert registry.select(["receive"], 601).id == "tempmail-lol"
    assert registry.select(["receive"], 172800).id == "tempmail-lol"
    with pytest.raises(ProviderError) as error:
        registry.select(["receive"], 172801)
    assert error.value.code == "TTL_UNSUPPORTED"


@pytest.mark.parametrize("ttl", [60, 172800, 31536000])
def test_created_mailbox_records_configured_capability_and_requested_local_expiry(settings, ttl):
    transport = ScriptedTransport(Response(200, {"mailbox": "long-lived@example.test", "token": "private-test-token"}), listing())
    provider = TempMailOrgProvider(max_ttl_seconds=31536000, session_factory=transport)
    with TestClient(create_app(settings, Registry([provider])), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        response = client.post("/v1/mailboxes", json={"ttl_seconds": ttl}, headers={"Idempotency-Key": "extended-ttl"})
        assert response.status_code == 202
        client.app.state.service.run_once()
        result = client.get(f"/v1/operations/{response.json()['id']}").json()
        assert result["status"] == "succeeded"
        mailbox = client.get(f"/v1/mailboxes/{result['result']['mailbox_id']}").json()
        lifetime = datetime.fromisoformat(mailbox["expires_at"]) - datetime.fromisoformat(mailbox["created_at"])
        assert lifetime.total_seconds() == pytest.approx(ttl, abs=1)
        assert mailbox["capabilities"]["max_ttl_seconds"] == 31536000


@pytest.mark.parametrize("ttl", [59, 31536001])
def test_creation_rejects_invalid_ttl_before_calling_provider(settings, ttl):
    transport = ScriptedTransport()
    provider = TempMailOrgProvider(max_ttl_seconds=31536000, session_factory=transport)
    with TestClient(create_app(settings, Registry([provider])), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        response = client.post("/v1/mailboxes", json={"ttl_seconds": ttl}, headers={"Idempotency-Key": str(ttl)})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert transport.calls == []


def test_lol_extended_local_lifetime_is_not_capped_by_old_default(settings):
    transport = ScriptedTransport(
        Response(200, {"address": "limited@example.test", "token": "private-test-token"}),
        Response(200, {"emails": [], "expired": False}),
    )
    provider = TempMailLolProvider(max_ttl_seconds=172800, session_factory=transport)
    with TestClient(create_app(settings, Registry([provider])), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        started = datetime.now(UTC)
        response = client.post("/v1/mailboxes", json={"ttl_seconds": 172800}, headers={"Idempotency-Key": "upstream-cap"})
        assert response.status_code == 202
        client.app.state.service.run_once()
        result = client.get(f"/v1/operations/{response.json()['id']}").json()
        assert result["status"] == "succeeded"
        mailbox = client.get(f"/v1/mailboxes/{result['result']['mailbox_id']}").json()
        lifetime = datetime.fromisoformat(mailbox["expires_at"]) - started
        assert lifetime.total_seconds() == pytest.approx(172800, abs=1)
        assert mailbox["capabilities"]["max_ttl_seconds"] == 172800


def test_actual_upstream_expiration_still_limits_requested_lifetime(settings, monkeypatch):
    provider = TempMailOrgProvider(max_ttl_seconds=172800)
    upstream_expiry = datetime.now(UTC) + timedelta(seconds=1800)
    upstream = ProviderMailbox("upstream-id", "limited@example.test", "private-test-token", upstream_expiry.isoformat())
    monkeypatch.setattr(provider, "create_mailbox", lambda ttl, request_id: upstream)
    monkeypatch.setattr(provider, "list_messages", lambda mailbox: [])
    with TestClient(create_app(settings, Registry([provider])), headers={"Authorization": f"Bearer {settings.api_token}"}) as client:
        response = client.post("/v1/mailboxes", json={"ttl_seconds": 172800}, headers={"Idempotency-Key": "real-upstream-expiry"})
        assert response.status_code == 202
        client.app.state.service.run_once()
        result = client.get(f"/v1/operations/{response.json()['id']}").json()
        assert result["status"] == "succeeded"
        mailbox = client.get(f"/v1/mailboxes/{result['result']['mailbox_id']}").json()
        assert datetime.fromisoformat(mailbox["expires_at"]) == upstream_expiry
        assert mailbox["capabilities"]["max_ttl_seconds"] == 172800
