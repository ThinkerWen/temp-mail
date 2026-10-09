from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest
import yaml
from cryptography.fernet import Fernet
from curl_cffi.requests.exceptions import RequestException
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ProviderSettings, Settings
from app.providers import factory
from app.providers.base import ProviderError, ProviderMailbox
from app.providers.registry import Registry
from app.providers.temp_mail_org import TempMailOrgProvider

EMAIL = "private@example.test"
TOKEN = "private-upstream-token"
BASE_URL = "https://upstream.example.test"


@dataclass
class Response:
    status_code: int
    data: object = None

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class ScriptedTransport:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []
        self.sessions = 0

    def __call__(self):
        self.sessions += 1
        return Session(self)


class Session:
    def __init__(self, transport):
        self.transport = transport

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, **kwargs):
        self.transport.calls.append({"method": method, "url": url, **kwargs})
        assert self.transport.responses, "Unexpected extra upstream request"
        response = self.transport.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "gateway.sqlite"),
        api_token="integration-api-token-with-enough-entropy",
        encryption_key=Fernet.generate_key().decode(),
    )


@pytest.fixture
def mailbox():
    return ProviderMailbox("mailbox-lifecycle-id", EMAIL, TOKEN)


def provider(transport):
    return TempMailOrgProvider(base_url=BASE_URL, timeout_seconds=3, session_factory=transport)


def listing(*message_ids, email=EMAIL):
    return Response(
        200, {"mailbox": email, "messages": [{"_id": message_id, "bodyPreview": "Only a preview"} for message_id in message_ids]}
    )


def detail(message_id, **fields):
    return Response(
        200,
        {
            "_id": message_id,
            "from": "sender@example.test",
            "subject": "Test mail",
            "receivedAt": 1_700_000_000,
            "bodyHtml": "<p>Hello</p>",
            **fields,
        },
    )


def api_client(settings, registry=None):
    return TestClient(create_app(settings, registry), headers={"Authorization": f"Bearer {settings.api_token}"})


def test_default_registry_construction_is_offline_and_advertises_receive_only(settings, monkeypatch):
    def unexpected_session(**kwargs):
        pytest.fail("Constructing the registry must not open an upstream session")

    monkeypatch.setattr("app.providers.temp_mail_org.requests.Session", unexpected_session)
    registry = factory.build_registry(settings)
    assert [item.id for item in registry.all()] == ["temp-mail-org", "tempmail-lol"]
    adapter = registry.get("temp-mail-org")
    assert adapter.impersonate == "chrome110"
    assert adapter.capabilities.receive is True
    assert adapter.capabilities.send is False
    assert adapter.capabilities.delete is False
    assert adapter.capabilities.attachments is False
    assert registry.select(["receive"], 3600).id == "temp-mail-org"
    disabled_settings = replace(settings, providers={**settings.providers, "temp-mail-org": ProviderSettings(enabled=False)})
    disabled = factory.build_registry(disabled_settings)
    assert [item.id for item in disabled.all()] == ["tempmail-lol"]


@pytest.mark.parametrize("proxy", ["", "  ", "http://user:password@127.0.0.1:7890", "socks5h://127.0.0.1:1080"])
def test_yaml_proxy_reaches_create_and_receive_sessions(settings, monkeypatch, tmp_path, proxy):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "app": {"db_path": "gateway.sqlite", "api_token": settings.api_token, "encryption_key": settings.encryption_key},
                "providers": {"temp-mail-org": {"enabled": True, "proxy": proxy, "base_url": BASE_URL, "timeout_seconds": 7}},
            }
        )
    )
    configured = Settings.from_yaml(config_path)
    transport = ScriptedTransport(Response(200, {"mailbox": EMAIL, "token": TOKEN}), listing())
    session_options = []

    def capture_session(**kwargs):
        session_options.append(kwargs)
        return transport()

    monkeypatch.setattr("app.providers.temp_mail_org.requests.Session", capture_session)
    adapter = factory.build_registry(configured).get("temp-mail-org")
    mailbox = adapter.create_mailbox(3600, "proxy-create")
    assert adapter.list_messages(mailbox) == []
    assert session_options == [{"impersonate": "chrome110", "proxy": proxy.strip() or None}] * 2
    assert [call["url"] for call in transport.calls] == [f"{BASE_URL}/mailbox", f"{BASE_URL}/messages"]
    assert all(call["timeout"] == 7 for call in transport.calls)
    assert "password" not in repr(configured)


def test_configured_proxy_failure_does_not_retry_directly(monkeypatch):
    transport = ScriptedTransport(RequestException("proxy credentials must not leak"))
    session_options = []

    def capture_session(**kwargs):
        session_options.append(kwargs)
        return transport()

    monkeypatch.setattr("app.providers.temp_mail_org.requests.Session", capture_session)
    adapter = TempMailOrgProvider(proxy="http://127.0.0.1:7890")
    with pytest.raises(ProviderError) as caught:
        adapter.create_mailbox(3600, "proxy-failure")
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert caught.value.uncertain is True
    assert session_options == [{"impersonate": "chrome110", "proxy": "http://127.0.0.1:7890"}]
    assert len(transport.calls) == 1
    assert "credentials" not in str(caught.value)


def test_invalid_proxy_is_rejected_without_exposing_credentials():
    with pytest.raises(ValueError) as caught:
        TempMailOrgProvider(proxy="http://user:secret@localhost:invalid")
    assert "secret" not in str(caught.value)


def test_create_uses_unauthenticated_empty_post_and_hashes_token_lifecycle():
    transport = ScriptedTransport(
        Response(200, {"mailbox": EMAIL, "token": TOKEN}),
        Response(200, {"mailbox": EMAIL, "token": "replacement-lifecycle-token"}),
        Response(200, {"mailbox": EMAIL, "token": TOKEN}),
    )
    adapter = provider(transport)
    first = adapter.create_mailbox(3600, "first")
    replacement = adapter.create_mailbox(3600, "second")
    same_token = adapter.create_mailbox(3600, "third")
    assert first.email == EMAIL
    assert first.credential == TOKEN
    assert first.expires_at is None
    assert first.upstream_id != TOKEN
    assert first.upstream_id != replacement.upstream_id
    assert first.upstream_id == same_token.upstream_id
    assert len(first.upstream_id) == 64
    for call in transport.calls:
        assert call["method"] == "POST"
        assert call["url"] == f"{BASE_URL}/mailbox"
        assert "Authorization" not in call["headers"]
        assert "json" not in call and "data" not in call
        assert call["timeout"] == 3
        assert call["allow_redirects"] is False
    assert transport.sessions == 3


def test_message_html_dates_encoded_paths_and_token_isolation(mailbox):
    message_id = "id/with?reserved#characters"
    html = (
        "<head><title>Hidden title</title></head><style>secret-style</style>"
        "<p>Hello &amp; welcome</p><script>secret-script</script><p>Next</p>"
    )
    second_email, second_token = "other@example.test", "different-mailbox-token"
    transport = ScriptedTransport(
        listing(message_id, message_id),
        detail(message_id, bodyHtml=html),
        listing("second", email=second_email),
        detail("second", bodyText="Plain text takes precedence", bodyHtml="<p>Fallback</p>"),
    )
    adapter = provider(transport)
    messages = adapter.list_messages(mailbox)
    assert len(messages) == 1
    assert messages[0].recipients == [EMAIL]
    assert messages[0].sender == "sender@example.test"
    assert "Hello & welcome" in messages[0].text and "Next" in messages[0].text
    assert "secret-script" not in messages[0].text and "secret-style" not in messages[0].text
    assert "Hidden title" not in messages[0].text and "<p>" not in messages[0].text
    assert messages[0].received_at == datetime.fromtimestamp(1_700_000_000, UTC).isoformat()
    assert transport.calls[1]["url"] == f"{BASE_URL}/messages/id%2Fwith%3Freserved%23characters"
    other = adapter.list_messages(ProviderMailbox("other-lifecycle", second_email, second_token))
    assert other[0].text == "Plain text takes precedence"
    assert other[0].recipients == [second_email]
    assert [call["headers"]["Authorization"] for call in transport.calls] == [
        f"Bearer {TOKEN}",
        f"Bearer {TOKEN}",
        f"Bearer {second_token}",
        f"Bearer {second_token}",
    ]
    assert all(call["method"] == "GET" and not call["allow_redirects"] for call in transport.calls)
    assert transport.sessions == 2


@pytest.mark.parametrize("plain_text", ["", " \n "])
def test_empty_plain_text_falls_back_to_html(mailbox, plain_text):
    transport = ScriptedTransport(listing("code"), detail("code", bodyText=plain_text, bodyHtml="<p>Your code is 123456</p>"))
    messages = provider(transport).list_messages(mailbox)
    assert messages[0].text == "Your code is 123456"


@pytest.mark.parametrize(
    ("status", "code", "uncertain"),
    [
        (401, "INVALID_CREDENTIAL", False),
        (403, "PROVIDER_ACCESS_DENIED", False),
        (408, "PROVIDER_UNAVAILABLE", True),
        (429, "PROVIDER_RATE_LIMITED", False),
        (503, "PROVIDER_UNAVAILABLE", True),
    ],
)
def test_creation_http_error_classification(status, code, uncertain):
    transport = ScriptedTransport(Response(status, {"token": "must-not-leak"}))
    with pytest.raises(ProviderError) as caught:
        provider(transport).create_mailbox(3600, "creation")
    assert caught.value.code == code
    assert caught.value.uncertain is uncertain
    assert "must-not-leak" not in str(caught.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("body", [{"mailbox": EMAIL}, ValueError("invalid JSON containing a secret")])
def test_invalid_successful_creation_response_is_uncertain(body):
    with pytest.raises(ProviderError) as caught:
        provider(ScriptedTransport(Response(200, body))).create_mailbox(3600, "creation")
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"
    assert caught.value.uncertain is True
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("creating", [True, False])
def test_network_exception_marks_only_creation_uncertain(mailbox, creating):
    transport = ScriptedTransport(RequestException(f"transport failed for token {TOKEN}"))
    adapter = provider(transport)
    with pytest.raises(ProviderError) as caught:
        adapter.create_mailbox(3600, "create") if creating else adapter.list_messages(mailbox)
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert caught.value.uncertain is creating
    assert TOKEN not in str(caught.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_disappeared_message_does_not_discard_other_messages(mailbox, status):
    transport = ScriptedTransport(listing("gone", "remaining"), Response(status), detail("remaining"))
    messages = provider(transport).list_messages(mailbox)
    assert [message.upstream_id for message in messages] == ["remaining"]
    assert messages[0].text == "Hello"


@pytest.mark.parametrize("mismatch", ["mailbox", "message_id"])
def test_mismatched_upstream_identity_is_rejected(mailbox, mismatch):
    responses = [listing("message", email="another@example.test")] if mismatch == "mailbox" else [listing("message"), detail("wrong-id")]
    with pytest.raises(ProviderError) as caught:
        provider(ScriptedTransport(*responses)).list_messages(mailbox)
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"
    assert caught.value.uncertain is False


def test_send_and_delete_are_rejected_without_network(mailbox):
    transport = ScriptedTransport()
    adapter = provider(transport)
    with pytest.raises(ProviderError, match="does not support sending") as sending:
        adapter.send_message(mailbox, ["recipient@example.test"], "Subject", "Body", "send")
    with pytest.raises(ProviderError) as deleting:
        adapter.delete_mailbox(mailbox, "delete")
    assert sending.value.code == deleting.value.code == "CAPABILITY_UNSUPPORTED"
    assert transport.sessions == 0
    assert transport.calls == []


def test_api_restart_restores_encrypted_binding_and_disabled_provider_never_falls_back(settings, monkeypatch):
    transport = ScriptedTransport(Response(200, {"mailbox": EMAIL, "token": TOKEN}), listing(), listing("mail"), detail("mail"))
    monkeypatch.setattr(factory, "TempMailOrgProvider", lambda **kwargs: TempMailOrgProvider(**kwargs, session_factory=transport))
    with api_client(settings) as client:
        response = client.post("/v1/mailboxes", json={"provider": "temp-mail-org"}, headers={"Idempotency-Key": "create"})
        assert response.status_code == 202
        client.app.state.service.run_once()
        operation = client.get(f"/v1/operations/{response.json()['id']}")
        assert operation.json()["status"] == "succeeded"
        mailbox_id = operation.json()["result"]["mailbox_id"]
        with client.app.state.service.db.connect() as db:
            encrypted = db.execute("SELECT credential_encrypted FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()[0]
        assert TOKEN not in encrypted
        assert Fernet(settings.encryption_key.encode()).decrypt(encrypted.encode()).decode() == TOKEN
        assert TOKEN not in operation.text
    with api_client(settings) as client:
        lookup = client.get("/v1/mailboxes", params={"email": EMAIL})
        assert lookup.json()["items"][0]["id"] == mailbox_id
        assert lookup.json()["items"][0]["provider_id"] == "temp-mail-org"
        assert TOKEN not in lookup.text and encrypted not in lookup.text
        assert client.app.state.service.sync_mailbox(mailbox_id) is True
        messages = client.get(f"/v1/mailboxes/{mailbox_id}/messages").json()["items"]
        assert len(messages) == 1 and "text" not in messages[0]
        message = client.get(f"/v1/mailboxes/{mailbox_id}/messages/{messages[0]['id']}")
        assert message.json()["text"] == "Hello"
        assert TOKEN not in message.text
    request_count = len(transport.calls)
    disabled_settings = replace(settings, providers={**settings.providers, "temp-mail-org": ProviderSettings(enabled=False)})
    with api_client(disabled_settings) as client:
        assert client.app.state.service.sync_mailbox(mailbox_id) is False
        mailbox_response = client.get(f"/v1/mailboxes/{mailbox_id}").json()
        assert mailbox_response["provider_id"] == "temp-mail-org"
        assert mailbox_response["last_sync_error_code"] == "PROVIDER_UNAVAILABLE"
        response = client.delete(f"/v1/mailboxes/{mailbox_id}", headers={"Idempotency-Key": "delete-disabled"})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert len(transport.calls) == request_count
    assert [call["headers"].get("Authorization") for call in transport.calls] == [
        None,
        f"Bearer {TOKEN}",
        f"Bearer {TOKEN}",
        f"Bearer {TOKEN}",
    ]


@pytest.mark.parametrize("uncertain", [True, False])
def test_creation_failure_state_is_persisted_without_automatic_retry(settings, uncertain):
    transport = ScriptedTransport(RequestException("request interrupted") if uncertain else Response(401))
    with api_client(settings, Registry([provider(transport)])) as client:
        headers, payload = {"Idempotency-Key": "create-once"}, {"provider": "temp-mail-org"}
        response = client.post("/v1/mailboxes", json=payload, headers=headers)
        assert response.status_code == 202
        client.app.state.service.run_once()
        result = client.get(f"/v1/operations/{response.json()['id']}").json()
        assert result["status"] == ("unknown" if uncertain else "failed")
        assert result["error_code"] == ("PROVIDER_UNAVAILABLE" if uncertain else "INVALID_CREDENTIAL")
        replay = client.post("/v1/mailboxes", json=payload, headers=headers)
        assert replay.json()["id"] == result["id"]
        assert replay.json()["status"] == result["status"]
        client.app.state.service.run_once()
        assert client.get("/v1/mailboxes").json()["items"] == []
    assert len(transport.calls) == 1


def test_detail_failure_does_not_cache_partial_results_or_previews(settings):
    transport = ScriptedTransport(
        Response(200, {"mailbox": EMAIL, "token": TOKEN}),
        listing(),
        listing("good", "failed"),
        detail("good"),
        Response(503),
        listing("good", "failed"),
        detail("good"),
        detail("failed", bodyText="Recovered full body"),
    )
    with api_client(settings, Registry([provider(transport)])) as client:
        response = client.post("/v1/mailboxes", json={"provider": "temp-mail-org"}, headers={"Idempotency-Key": "create"})
        client.app.state.service.run_once()
        operation = client.get(f"/v1/operations/{response.json()['id']}").json()
        mailbox_id = operation["result"]["mailbox_id"]
        assert client.app.state.service.sync_mailbox(mailbox_id) is False
        assert client.get(f"/v1/mailboxes/{mailbox_id}/messages").json()["items"] == []
        assert client.get(f"/v1/mailboxes/{mailbox_id}").json()["last_sync_error_code"] == "PROVIDER_UNAVAILABLE"
        assert client.app.state.service.sync_mailbox(mailbox_id) is True
        messages = client.get(f"/v1/mailboxes/{mailbox_id}/messages").json()["items"]
        assert len(messages) == 2
        bodies = [client.get(f"/v1/mailboxes/{mailbox_id}/messages/{message['id']}").json()["text"] for message in messages]
        assert set(bodies) == {"Hello", "Recovered full body"}
        assert client.get(f"/v1/mailboxes/{mailbox_id}").json()["last_sync_error_code"] is None
