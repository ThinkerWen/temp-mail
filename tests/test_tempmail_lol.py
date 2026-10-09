from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from cryptography.fernet import Fernet
from curl_cffi.requests.exceptions import RequestException
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.providers.base import ProviderError, ProviderMailbox
from app.providers.factory import build_registry
from app.providers.registry import Registry
from app.providers.tempmail_lol import TempMailLolProvider

EMAIL = "private@inbox.example.test"
TOKEN = "private/token+with&reserved?characters=#"
BASE_URL = "https://upstream.example.test/v2"


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
        self.session_options = []

    def __call__(self, **kwargs):
        self.session_options.append(kwargs)
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
        api_token="test-api-token-with-enough-entropy",
        encryption_key=Fernet.generate_key().decode(),
    )


@pytest.fixture
def mailbox():
    return ProviderMailbox("mailbox-lifecycle", EMAIL, TOKEN)


def adapter(transport):
    return TempMailLolProvider(base_url=BASE_URL, timeout_seconds=3, session_factory=transport)


def message(**changes):
    return {
        "from": "sender@example.test",
        "to": EMAIL,
        "subject": "Test mail",
        "date": 1_700_000_000_123,
        "body": "Full message body",
        **changes,
    }


def test_yaml_registers_receive_only_lol_provider_in_configured_order(settings, tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "app": {"db_path": settings.db_path, "api_token": settings.api_token, "encryption_key": settings.encryption_key},
                "providers": {"tempmail-lol": {"enabled": True}, "temp-mail-org": {"enabled": True}},
            },
            sort_keys=False,
        )
    )

    def unexpected_session(**kwargs):
        pytest.fail("Provider registration must not open a network session")

    monkeypatch.setattr("app.providers.tempmail_lol.requests.Session", unexpected_session)
    configured = Settings.from_yaml(config_path)
    registry = build_registry(configured)
    assert [provider.id for provider in registry.all()] == ["tempmail-lol", "temp-mail-org"]
    provider = registry.get("tempmail-lol")
    assert provider.capabilities.receive is True
    assert provider.capabilities.destructive_receive is True
    assert provider.capabilities.send is False
    assert provider.capabilities.delete is False
    assert provider.capabilities.attachments is False
    assert provider.capabilities.max_ttl_seconds == 3600
    assert registry.select(["receive"], 3600).id == "tempmail-lol"
    assert registry.select(["receive"], 3601).id == "temp-mail-org"


@pytest.mark.parametrize("proxy", [None, "http://user:private-proxy-secret@localhost:7890", "socks5h://localhost:1080"])
def test_yaml_proxy_and_browser_profile_reach_create_and_receive_without_raw_token_urls(settings, tmp_path, monkeypatch, proxy):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "app": {"db_path": settings.db_path, "api_token": settings.api_token, "encryption_key": settings.encryption_key},
                "providers": {"tempmail-lol": {"proxy": proxy, "base_url": BASE_URL, "timeout_seconds": 3}},
            }
        )
    )
    transport = ScriptedTransport(Response(200, {"address": EMAIL, "token": TOKEN}), Response(200, {"emails": [], "expired": False}))
    monkeypatch.setattr("app.providers.tempmail_lol.requests.Session", transport)
    configured = Settings.from_yaml(config_path)
    provider = build_registry(configured).get("tempmail-lol")
    before = datetime.now(UTC)
    created = provider.create_mailbox(600, "create")
    after = datetime.now(UTC)
    assert created.email == EMAIL and created.credential == TOKEN
    assert TOKEN not in created.upstream_id and created.upstream_id != EMAIL
    assert before + timedelta(seconds=3600) <= datetime.fromisoformat(created.expires_at) <= after + timedelta(seconds=3600)
    assert provider.list_messages(created) == []
    assert transport.session_options == [{"impersonate": "chrome110", "proxy": proxy}] * 2
    post, get = transport.calls
    assert post["method"] == "POST" and post["url"] == f"{BASE_URL}/inbox/create"
    assert post["json"] == {"domain": None, "captcha": None}
    assert "Authorization" not in post["headers"]
    assert get["method"] == "GET" and get["url"] == f"{BASE_URL}/inbox"
    assert get["params"] == {"token": TOKEN}
    assert TOKEN not in get["url"]
    assert all(call["timeout"] == 3 and call["allow_redirects"] is False for call in transport.calls)
    assert "private-proxy-secret" not in repr(configured)


def test_excessive_ttl_and_unsupported_mutations_do_not_call_network(mailbox):
    transport = ScriptedTransport()
    provider = adapter(transport)
    with pytest.raises(ProviderError) as ttl:
        provider.create_mailbox(3601, "too-long")
    assert ttl.value.code == "TTL_UNSUPPORTED"
    with pytest.raises(ProviderError) as sending:
        provider.send_message(mailbox, [EMAIL], "Subject", "Text", "send")
    with pytest.raises(ProviderError) as deleting:
        provider.delete_mailbox(mailbox, "delete")
    assert sending.value.code == deleting.value.code == "CAPABILITY_UNSUPPORTED"
    assert transport.calls == []
    assert transport.session_options == []


@pytest.mark.parametrize(
    ("status", "code", "uncertain"),
    [
        (401, "INVALID_CREDENTIAL", False),
        (403, "PROVIDER_ACCESS_DENIED", False),
        (429, "PROVIDER_RATE_LIMITED", False),
        (503, "PROVIDER_UNAVAILABLE", True),
        (408, "PROVIDER_UNAVAILABLE", True),
    ],
)
def test_http_errors_are_classified_without_leaking_response_secrets(status, code, uncertain):
    transport = ScriptedTransport(Response(status, {"error": f"upstream secret {TOKEN}"}))
    with pytest.raises(ProviderError) as caught:
        adapter(transport).create_mailbox(3600, "create")
    assert caught.value.code == code
    assert caught.value.uncertain is uncertain
    assert TOKEN not in str(caught.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("body", [{"address": EMAIL}, ValueError("private-response-content")])
def test_malformed_successful_creation_is_uncertain(body):
    with pytest.raises(ProviderError) as caught:
        adapter(ScriptedTransport(Response(200, body))).create_mailbox(3600, "create")
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"
    assert caught.value.uncertain is True
    assert "private-response-content" not in str(caught.value)


@pytest.mark.parametrize("creating", [True, False])
def test_create_and_destructive_receive_network_failures_are_uncertain(mailbox, creating):
    transport = ScriptedTransport(RequestException(f"request failed with credential {TOKEN}"))
    provider = adapter(transport)
    with pytest.raises(ProviderError) as caught:
        provider.create_mailbox(3600, "create") if creating else provider.fetch_messages(mailbox)
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert caught.value.uncertain is True
    assert TOKEN not in str(caught.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("payload", "code"),
    [({"captcha_required": True}, "CAPTCHA_REQUIRED"), ({"error": "private-upstream-error"}, "PROVIDER_REQUEST_REJECTED")],
)
def test_explicit_creation_rejection_is_definite(payload, code):
    with pytest.raises(ProviderError) as caught:
        adapter(ScriptedTransport(Response(200, payload))).create_mailbox(3600, "create")
    assert caught.value.code == code
    assert caught.value.uncertain is False
    assert "private-upstream-error" not in str(caught.value)


def test_millisecond_dates_html_conversion_and_recipient_mapping(mailbox):
    payload = {
        "emails": [message(id="upstream-id", body=None, html="<p>Hello &amp; welcome</p><script>hidden-script</script><p>Next</p>")],
        "expired": False,
    }
    parsed = adapter(ScriptedTransport()).parse_messages(mailbox, payload, "batch-one")
    assert len(parsed) == 1
    assert parsed[0].upstream_id == "upstream-id"
    assert parsed[0].recipients == [EMAIL]
    assert parsed[0].received_at == "2023-11-14T22:13:20.123000+00:00"
    assert "Hello & welcome" in parsed[0].text and "Next" in parsed[0].text
    assert "hidden-script" not in parsed[0].text and "<p>" not in parsed[0].text


def test_identical_messages_without_ids_remain_distinct_and_batch_replay_is_stable(mailbox):
    provider = adapter(ScriptedTransport())
    payload = {"emails": [message(), message()], "expired": False}
    first = provider.parse_messages(mailbox, payload, "first-batch")
    replay = provider.parse_messages(mailbox, payload, "first-batch")
    second = provider.parse_messages(mailbox, payload, "second-batch")
    assert [mail.upstream_id for mail in first] == ["first-batch:0", "first-batch:1"]
    assert [mail.upstream_id for mail in replay] == [mail.upstream_id for mail in first]
    assert len({mail.upstream_id for mail in first + second}) == 4
    assert first[0].text == first[1].text


def test_raw_fetch_preserves_payload_for_staging_before_schema_validation(mailbox):
    raw = {"emails": {"unexpected": "format"}, "expired": False, "extra": "preserve-for-recovery"}
    provider = adapter(ScriptedTransport(Response(200, raw)))
    fetched = provider.fetch_messages(mailbox)
    assert fetched == raw
    with pytest.raises(ProviderError) as caught:
        provider.parse_messages(mailbox, fetched, "batch")
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"error": "private-upstream-error"}, "PROVIDER_REQUEST_REJECTED"),
        ({"emails": [], "captcha_required": True}, "CAPTCHA_REQUIRED"),
        ({"emails": None, "expired": True}, "MAILBOX_EXPIRED"),
    ],
)
def test_fetch_rejects_error_envelopes_without_messages_before_staging(mailbox, payload, code):
    transport = ScriptedTransport(Response(200, payload))
    with pytest.raises(ProviderError) as caught:
        adapter(transport).fetch_messages(mailbox)
    assert caught.value.code == code
    assert caught.value.uncertain is False
    assert "private-upstream-error" not in str(caught.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("envelope", [{"error": "upstream-error"}, {"captcha_required": True}, {"expired": True}])
def test_fetch_preserves_nonempty_messages_with_error_envelopes_for_staging(mailbox, envelope):
    payload = {"emails": [message()], **envelope}
    transport = ScriptedTransport(Response(200, payload))
    assert adapter(transport).fetch_messages(mailbox) == payload
    assert len(transport.calls) == 1


@pytest.mark.parametrize("changes", [{"to": "wrong-mailbox@example.test"}, {"date": "not-a-unix-millisecond-timestamp"}])
def test_malformed_message_fields_are_rejected(mailbox, changes):
    payload = {"emails": [message(**changes)], "expired": False}
    with pytest.raises(ProviderError) as caught:
        adapter(ScriptedTransport()).parse_messages(mailbox, payload, "batch")
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"


def test_expired_inbox_is_reported_explicitly(mailbox):
    provider = adapter(ScriptedTransport(Response(200, {"emails": [], "expired": True})))
    with pytest.raises(ProviderError) as caught:
        provider.list_messages(mailbox)
    assert caught.value.code == "MAILBOX_EXPIRED"
    assert caught.value.uncertain is False


def test_unknown_creation_is_persisted_and_idempotent_replay_does_not_retry(settings):
    transport = ScriptedTransport(RequestException("ambiguous upstream outcome"))
    with TestClient(
        create_app(settings, Registry([adapter(transport)])), headers={"Authorization": f"Bearer {settings.api_token}"}
    ) as client:
        payload, headers = {"provider": "tempmail-lol"}, {"Idempotency-Key": "ambiguous-create"}
        response = client.post("/v1/mailboxes", json=payload, headers=headers)
        assert response.status_code == 202, response.text
        client.app.state.service.run_once()
        result = client.get(f"/v1/operations/{response.json()['id']}").json()
        assert result["status"] == "unknown"
        assert result["error_code"] == "PROVIDER_UNAVAILABLE"
        replay = client.post("/v1/mailboxes", json=payload, headers=headers)
        assert replay.json()["id"] == result["id"]
        assert replay.json()["status"] == "unknown"
        client.app.state.service.run_once()
        assert client.get("/v1/mailboxes").json()["items"] == []
    assert len(transport.calls) == 1
