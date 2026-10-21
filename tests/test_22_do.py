import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from curl_cffi.requests.exceptions import RequestException
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ProviderSettings, Settings
from app.providers.base import ProviderError, ProviderMailbox
from app.providers.registry import Registry
from app.providers.twenty_two_do import TwentyTwoDoProvider
from tests.fakes import Response, ScriptedTransport

EMAIL = "test+unique@example.test"
TOKEN = "private-mailbox-token"
BASE = "https://provider.example.test"
NOW = 1_800_000_000
UUID = "a" * 32


def html(value, status=200):
    response = Response(status)
    response.text = value
    return response


def token_response(token=TOKEN, expires=NOW + 86400):
    return Response(200, {"status": True, "data": {"token": token, "expireTime": expires}})


def item(message_id="message", **fields):
    return {"messageId": message_id, "time": NOW + 1, "from": "Sender <sender@example.test>", "to": EMAIL, "subject": "Test", **fields}


def listing(*messages):
    return Response(200, {"status": True, "data": list(messages)})


def detail(source="/view/body-token"):
    return html(f'<html><h1>Navigation is not the body</h1><iframe src="{source}"></iframe></html>')


def provider(transport, **options):
    return TwentyTwoDoProvider(base_url=BASE, timeout_seconds=3, session_factory=transport, **options)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr("app.providers.twenty_two_do.time.time", lambda: NOW)


@pytest.fixture
def mailbox():
    credential = json.dumps({"email": EMAIL, "uuid": UUID, "token": TOKEN, "token_expires_at": NOW + 7200, "created_at": NOW})
    return ProviderMailbox(UUID, EMAIL, credential, datetime.fromtimestamp(NOW + 86400, UTC).isoformat())


def test_create_parses_page_and_keeps_token_expiry_separate_from_mailbox_lifetime():
    transport = ScriptedTransport(html(f'<script>let email = "{EMAIL}"; let timeLeft = 600;</script>'), token_response(expires=NOW + 7200))
    created = provider(transport).create_mailbox(3600, "operation-id")
    credential = json.loads(created.credential)
    assert created.email == EMAIL
    assert created.upstream_id == credential["uuid"] and len(created.upstream_id) == 32
    assert credential["created_at"] == NOW
    assert credential["token"] == TOKEN
    assert credential["token_expires_at"] == NOW + 7200
    assert created.expires_at == datetime.fromtimestamp(NOW + 600, UTC).isoformat()
    assert [call["url"] for call in transport.calls] == [f"{BASE}/inbox", f"{BASE}/action/mailbox/applyToken"]
    assert transport.calls[1]["json"] == {"email": EMAIL, "uuid": created.upstream_id}
    assert all(not call["allow_redirects"] and call["timeout"] == 3 for call in transport.calls)
    assert all("Authorization" not in call["headers"] for call in transport.calls)


@pytest.mark.parametrize("empty", [None, False, []])
def test_empty_inbox_preserves_creation_watermark_and_complete_alias(mailbox, empty):
    transport = ScriptedTransport(Response(200, {"status": True, "data": empty}))
    assert provider(transport).list_messages(mailbox) == []
    assert transport.calls[0]["json"] == {"email": EMAIL, "lastime": NOW}
    assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_receives_iframe_body_filters_old_and_other_aliases_and_deduplicates(mailbox):
    transport = ScriptedTransport(
        listing(item("old", time=NOW - 1), item("other", to="test+other@example.test"), item(), item()),
        html('<iframe src="https://ads.example.test/ad"></iframe><iframe src="/view/message-body"></iframe>'),
        html("<head><title>Hidden</title></head><p>can you receive?</p><p>你好 &amp; hello</p><script>hidden()</script>"),
    )
    messages = provider(transport).list_messages(mailbox)
    assert len(messages) == 1
    message = messages[0]
    assert message.upstream_id == "message" and message.recipients == [EMAIL]
    assert message.subject == "Test" and message.sender == "Sender <sender@example.test>"
    assert message.received_at == datetime.fromtimestamp(NOW + 1, UTC).isoformat()
    assert message.text == "can you receive?\n\n你好 & hello"
    assert [call["url"] for call in transport.calls] == [
        f"{BASE}/action/mailbox/message",
        f"{BASE}/content/message",
        f"{BASE}/view/message-body",
    ]
    assert all("Authorization" not in call["headers"] for call in transport.calls[1:])
    assert all(call["allow_redirects"] is False for call in transport.calls)


@pytest.mark.parametrize(
    "source",
    [
        "https://outside.example.test/view/token",
        "http://provider.example.test/view/token",
        "//localhost/view/token",
        "https://user:secret@provider.example.test/view/token",
        "/view/%2e%2e/admin",
        "/view/token?next=secret",
        "/view/token#fragment",
        "/view/",
        "javascript:alert(1)",
        "/admin",
    ],
)
def test_rejects_untrusted_or_invalid_iframe_without_fetching_it(mailbox, source):
    transport = ScriptedTransport(listing(item()), detail(source))
    with pytest.raises(ProviderError) as caught:
        provider(transport).list_messages(mailbox)
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"
    assert len(transport.calls) == 2
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("page", ["<h1>Login required</h1>", '<iframe src="/view/one"></iframe><iframe src="/view/two"></iframe>'])
def test_missing_or_ambiguous_body_is_not_cached_as_mail_text(mailbox, page):
    with pytest.raises(ProviderError) as caught:
        provider(ScriptedTransport(listing(item()), html(page))).list_messages(mailbox)
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"


@pytest.mark.parametrize("challenge_header", [False, True])
def test_body_challenge_is_rejected_and_same_message_can_be_retried(mailbox, challenge_header):
    blocked = html("<title>Just a moment...</title><h1>Verify you are human</h1>")
    if challenge_header:
        blocked.text = "<html>Blocked response</html>"
        blocked.headers = {"cf-mitigated": "challenge"}
    transport = ScriptedTransport(listing(item()), detail(), blocked, listing(item()), detail(), html("<p>Recovered body</p>"))
    adapter = provider(transport)
    with pytest.raises(ProviderError) as caught:
        adapter.list_messages(mailbox)
    assert caught.value.code in {"PROVIDER_ACCESS_DENIED", "PROVIDER_INVALID_RESPONSE"}
    assert not caught.value.uncertain
    assert adapter.list_messages(mailbox)[0].text == "Recovered body"


def test_normal_verification_email_is_not_misidentified_as_challenge(mailbox):
    transport = ScriptedTransport(
        listing(item()), detail(), html("<title>Verify your email</title><p>Verify you are human. Your login code is 123456.</p>")
    )
    assert "Your login code is 123456" in provider(transport).list_messages(mailbox)[0].text


def test_expired_token_refreshes_once_and_survives_new_adapter_instance(mailbox):
    credential = json.loads(mailbox.credential)
    credential["token_expires_at"] = NOW - 1
    mailbox = replace(mailbox, credential=json.dumps(credential))
    transport = ScriptedTransport(token_response("renewed-token"), listing(), listing(), token_response("restart-token"), listing())
    adapter = provider(transport)
    assert adapter.list_messages(mailbox) == adapter.list_messages(mailbox) == []
    assert provider(transport).list_messages(mailbox) == []
    assert transport.calls[0]["json"] == transport.calls[3]["json"] == {"email": EMAIL, "uuid": UUID}
    assert [transport.calls[i]["headers"]["Authorization"] for i in (1, 2, 4)] == [
        "Bearer renewed-token",
        "Bearer renewed-token",
        "Bearer restart-token",
    ]
    assert all(call["method"] == "POST" for call in transport.calls)


def test_rejected_token_refreshes_once_without_recreating_mailbox(mailbox):
    transport = ScriptedTransport(Response(401), token_response("new-token"), Response(401))
    with pytest.raises(ProviderError) as caught:
        provider(transport).list_messages(mailbox)
    assert caught.value.code == "INVALID_CREDENTIAL" and not caught.value.uncertain
    assert len(transport.calls) == 3
    assert transport.calls[2]["headers"]["Authorization"] == "Bearer new-token"


@pytest.mark.parametrize("status,code", [(401, "INVALID_CREDENTIAL"), (403, "PROVIDER_ACCESS_DENIED"), (429, "PROVIDER_RATE_LIMITED")])
def test_definitive_initial_rejection_is_not_uncertain(status, code):
    transport = ScriptedTransport(Response(status))
    with pytest.raises(ProviderError) as caught:
        provider(transport).create_mailbox(3600, "create")
    assert caught.value.code == code and not caught.value.uncertain
    assert len(transport.calls) == 1


@pytest.mark.parametrize("response", [Response(503), Response(302), html("unexpected website"), RequestException("secret transport")])
def test_ambiguous_creation_is_not_retried(response):
    transport = ScriptedTransport(response)
    with pytest.raises(ProviderError) as caught:
        provider(transport).create_mailbox(3600, "create")
    assert caught.value.uncertain and len(transport.calls) == 1
    assert "secret" not in str(caught.value)


def test_token_failure_after_allocation_keeps_creation_uncertain():
    transport = ScriptedTransport(html(f'let email="{EMAIL}";let timeLeft=86400;'), Response(429))
    with pytest.raises(ProviderError) as caught:
        provider(transport).create_mailbox(3600, "create")
    assert caught.value.code == "PROVIDER_RATE_LIMITED" and caught.value.uncertain
    assert len(transport.calls) == 2


@pytest.mark.parametrize("data", [True, {}, "not a list", [None], [item(time=True)], [item(to=None)], [item(messageId="..")]])
def test_invalid_lists_fail_without_leaking_upstream_data(mailbox, data):
    with pytest.raises(ProviderError) as caught:
        provider(ScriptedTransport(Response(200, {"status": True, "data": data}))).list_messages(mailbox)
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"


@pytest.mark.parametrize("status", [404, 410])
def test_missing_message_does_not_prevent_reading_remaining_mail(mailbox, status):
    transport = ScriptedTransport(listing(item("gone"), item("remaining")), Response(status), detail(), html("<p>Present</p>"))
    messages = provider(transport).list_messages(mailbox)
    assert [(m.upstream_id, m.text) for m in messages] == [("remaining", "Present")]


def test_invalid_credential_and_unsupported_operations_never_open_network(mailbox):
    transport = ScriptedTransport()
    adapter = provider(transport)
    with pytest.raises(ProviderError) as caught:
        adapter.list_messages(replace(mailbox, credential='{"token":"secret"}'))
    assert caught.value.code == "INVALID_CREDENTIAL" and "secret" not in str(caught.value)
    with pytest.raises(ProviderError) as caught:
        adapter.send_message(mailbox, [EMAIL], "Subject", "Body", "send")
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"
    with pytest.raises(ProviderError) as caught:
        adapter.delete_mailbox(mailbox, "delete")
    assert caught.value.code == "CAPABILITY_UNSUPPORTED"
    assert transport.sessions == 0


def test_configured_proxy_and_impersonation_apply_to_every_session(monkeypatch, mailbox):
    transport = ScriptedTransport(html(f'let email="{EMAIL}";let timeLeft=86400;'), token_response(), listing())
    monkeypatch.setattr("app.providers.twenty_two_do.requests.Session", transport)
    adapter = TwentyTwoDoProvider(proxy="http://user:private@localhost:7890", impersonate="chrome110")
    adapter.create_mailbox(3600, "create")
    adapter.list_messages(mailbox)
    assert transport.session_options == [{"proxy": "http://user:private@localhost:7890", "impersonate": "chrome110"}] * 2


def test_api_restart_restores_binding_and_caches_body_without_credentials(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_token="test-api-token-long-enough",
        encryption_key=Fernet.generate_key().decode(),
        providers={"22-do": ProviderSettings()},
    )
    transport = ScriptedTransport(html(f'let email="{EMAIL}";let timeLeft=86400;'), token_response(), listing())
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(create_app(settings, Registry([provider(transport)])), headers=headers) as client:
        response = client.post("/v1/mailboxes", json={"provider": "22-do", "ttl_seconds": 3600}, headers={"Idempotency-Key": "create"})
        assert response.status_code == 202
        client.app.state.service.run_once()
        operation = client.get(f"/v1/operations/{response.json()['id']}").json()
        assert operation["status"] == "succeeded"
        mailbox_id = operation["result"]["mailbox_id"]
        with client.app.state.service.db.connect() as db:
            row = db.execute("SELECT credential_encrypted FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
        assert TOKEN not in row[0]
        credential = json.loads(Fernet(settings.encryption_key.encode()).decrypt(row[0].encode()))
        assert credential["token"] == TOKEN and credential["email"] == EMAIL
    transport = ScriptedTransport(
        listing(item()), detail(), html("<p>can you receive?</p>"), listing(item()), detail(), html("<p>can you receive?</p>")
    )
    with TestClient(create_app(settings, Registry([provider(transport)])), headers=headers) as client:
        assert client.app.state.service.sync_mailbox(mailbox_id)
        assert client.app.state.service.sync_mailbox(mailbox_id)
        messages = client.get(f"/v1/mailboxes/{mailbox_id}/messages").json()["items"]
        assert len(messages) == 1
        response = client.get(f"/v1/mailboxes/{mailbox_id}/messages/{messages[0]['id']}")
        assert response.json()["text"] == "can you receive?"
        assert TOKEN not in response.text and UUID not in response.text
        assert client.get(f"/v1/mailboxes/{mailbox_id}").json()["provider_id"] == "22-do"
