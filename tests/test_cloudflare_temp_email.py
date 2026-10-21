import hashlib
from dataclasses import replace
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet
from curl_cffi.requests.exceptions import RequestException
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import ProviderSettings, Settings
from app.providers.base import ProviderError, ProviderMailbox
from app.providers.cloudflare_temp_email import CloudflareTempEmailProvider
from app.providers.registry import Registry
from tests.fakes import Response, ScriptedTransport

BASE = "https://worker.example.test"
EMAIL = "receiver+tag@example.test"
TOKEN = "private-address-jwt"
ADMIN = "private-admin-password"
SITE = "private-site-password"
RAW = (
    "From: Sender <sender@example.test>\r\n"
    "To: receiver+tag@example.test\r\n"
    "Subject: =?utf-8?b?5rWL6K+V?=\r\n"
    "Date: Wed, 21 Oct 2026 10:00:00 +0800\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "Content-Transfer-Encoding: base64\r\n\r\n"
    "5L2g5aW977yM5LiW55WM\r\n"
)


def created():
    return Response(200, {"address": EMAIL, "jwt": TOKEN, "address_id": 12, "password": "unused-address-password"})


def row(identifier=1, **fields):
    return {
        "id": identifier,
        "address": EMAIL,
        "raw": RAW,
        "source": "envelope@example.test",
        "created_at": "2026-10-21 02:00:00",
        **fields,
    }


def page(*rows, count=None):
    return Response(200, {"results": list(rows), "count": len(rows) if count is None else count})


def provider(transport, **options):
    return CloudflareTempEmailProvider(base_url=BASE, session_factory=transport, **options)


@pytest.fixture
def mailbox():
    return ProviderMailbox(hashlib.sha256(TOKEN.encode()).hexdigest(), EMAIL, TOKEN)


@pytest.mark.parametrize("domain", [None, "example.test"])
def test_public_create_returns_independent_address_jwt_and_uses_optional_domain(domain):
    transport = ScriptedTransport(created())
    adapter = provider(transport, domain=domain)
    mailbox = adapter.create_mailbox(3600, "create")
    assert mailbox.email == EMAIL and mailbox.credential == TOKEN
    assert mailbox.upstream_id == hashlib.sha256(TOKEN.encode()).hexdigest()
    assert mailbox.expires_at is None
    assert adapter.capabilities.receive and not adapter.capabilities.send and not adapter.capabilities.delete
    call = transport.calls[0]
    assert call["method"] == "POST" and call["url"] == f"{BASE}/api/new_address"
    assert call["json"] == ({"domain": domain} if domain else {})
    assert "x-admin-auth" not in call["headers"] and "Authorization" not in call["headers"]
    assert not call["allow_redirects"]


def test_admin_create_and_receive_keep_authentication_scopes_separate():
    transport = ScriptedTransport(created(), page())
    adapter = provider(transport, domain="example.test", admin_password=ADMIN, site_password=SITE)
    mailbox = adapter.create_mailbox(3600, "create")
    assert adapter.list_messages(mailbox) == []
    create, receive = transport.calls
    assert create["url"] == f"{BASE}/admin/new_address"
    assert create["headers"]["x-admin-auth"] == ADMIN
    assert create["json"]["enablePrefix"] is False
    assert 1 <= len(create["json"]["name"]) <= 30
    assert create["json"]["domain"] == "example.test"
    assert "Authorization" not in create["headers"]
    assert receive["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "x-admin-auth" not in receive["headers"]
    assert all(call["headers"]["x-custom-auth"] == SITE for call in transport.calls)


def test_placeholder_is_rejected_before_opening_network():
    transport = ScriptedTransport()
    adapter = CloudflareTempEmailProvider(session_factory=transport)
    with pytest.raises(ProviderError) as caught:
        adapter.create_mailbox(3600, "create")
    assert caught.value.code == "PROVIDER_NOT_CONFIGURED" and not caught.value.uncertain
    assert transport.sessions == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("admin_password", "secret\nheader"),
        ("site_password", "secret\rheader"),
        ("domain", "https://bad.test"),
        ("base_url", "https://user:secret@worker.test"),
    ],
)
def test_invalid_configuration_does_not_expose_secret_values(field, value):
    with pytest.raises(ValueError) as caught:
        CloudflareTempEmailProvider(**{field: value})
    assert "secret" not in str(caught.value)


def test_pagination_retains_first_count_when_later_pages_report_zero(mailbox):
    transport = ScriptedTransport(page(row(3), count=3), page(row(2), count=0), page(row(1), count=0))
    messages = provider(transport).list_messages(mailbox)
    assert [message.upstream_id for message in messages] == ["3", "2", "1"]
    assert [call["params"] for call in transport.calls] == [{"limit": 100, "offset": offset} for offset in range(3)]
    assert all(message.subject == "测试" and message.text.strip() == "你好，世界" for message in messages)
    assert all(message.recipients == [EMAIL] for message in messages)
    assert messages[0].received_at == "2026-10-21T02:00:00+00:00"


@pytest.mark.parametrize("body", ["你好，世界", "café", "你好 café 📧"])
def test_utf8_8bit_mime_preserves_unicode_body(mailbox, body):
    raw = (
        "From: sender@example.test\r\nSubject: Unicode mail\r\nMIME-Version: 1.0\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n{body}\r\n"
    )
    result = provider(ScriptedTransport(page(row(raw=raw)))).list_messages(mailbox)[0]
    assert result.text.strip() == body


def test_multipart_prefers_text_ignores_attachments_and_accepts_bcc_delivery(mailbox):
    message = EmailMessage()
    message["From"] = "发件人 <sender@example.test>"
    message["To"] = "other@example.test"
    message["Subject"] = "多种正文"
    message.set_content("纯文本正文", charset="utf-8", cte="quoted-printable")
    message.add_alternative("<p>HTML alternative</p>", subtype="html")
    message.add_attachment("Private attachment", subtype="plain", filename="attachment.txt")
    messages = provider(ScriptedTransport(page(row(raw=message.as_string())))).list_messages(mailbox)
    assert messages[0].text.strip() == "纯文本正文"
    assert messages[0].recipients == [EMAIL]
    assert "发件人" in messages[0].sender


def test_html_only_body_is_plain_text_and_missing_storage_date_uses_mail_date(mailbox):
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["Date"] = "Wed, 21 Oct 2026 10:00:00 +0800"
    message.set_content(
        '<head><title>hidden title</title></head><p>Hello &amp; world</p><script>hidden script</script><img src="https://tracking.test/pixel">',
        subtype="html",
    )
    transport = ScriptedTransport(page(row(raw=message.as_string(), created_at=None)))
    result = provider(transport).list_messages(mailbox)[0]
    assert result.text == "Hello & world"
    assert result.received_at == "2026-10-21T02:00:00+00:00"
    assert len(transport.calls) == 1


def test_attachment_only_mail_does_not_treat_attached_email_as_body(mailbox):
    attached = EmailMessage()
    attached["Subject"] = "Attached email"
    attached.set_content("Attachment secret")
    message = EmailMessage()
    message["Subject"] = "No main body"
    message.add_attachment(attached, filename="forwarded.eml")
    result = provider(ScriptedTransport(page(row(raw=message.as_string())))).list_messages(mailbox)[0]
    assert result.subject == "No main body" and result.text == ""


@pytest.mark.parametrize("missing", [Response(200, None), Response(404), Response(410)])
def test_missing_raw_fetches_detail_and_skips_disappeared_messages(mailbox, missing):
    transport = ScriptedTransport(page(row(1, raw=None), row(2, raw=None), count=2), missing, Response(200, row(2)))
    messages = provider(transport, site_password=SITE).list_messages(mailbox)
    assert [message.upstream_id for message in messages] == ["2"]
    assert [call["url"] for call in transport.calls[1:]] == [f"{BASE}/api/mail/1", f"{BASE}/api/mail/2"]
    assert all(call["headers"]["Authorization"] == f"Bearer {TOKEN}" for call in transport.calls)
    assert all(call["headers"]["x-custom-auth"] == SITE for call in transport.calls)


@pytest.mark.parametrize(
    "record", [row(address="other@example.test"), row(id=".."), row(id=True), row(raw=3), row(created_at="not-a-date")]
)
def test_invalid_identity_or_message_data_fails_without_caching(mailbox, record):
    transport = ScriptedTransport(page(record))
    with pytest.raises(ProviderError) as caught:
        provider(transport).list_messages(mailbox)
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE" and not caught.value.uncertain
    assert len(transport.calls) == 1


@pytest.mark.parametrize("record", [row(2), row(1, address="other@example.test")])
def test_detail_identity_must_match_list_and_bound_mailbox(mailbox, record):
    with pytest.raises(ProviderError) as caught:
        provider(ScriptedTransport(page(row(1, raw=None)), Response(200, record))).list_messages(mailbox)
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "raw", ["", "not an RFC822 message", 'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="missing"\r\n\r\nTruncated']
)
def test_malformed_mime_is_not_silently_cached_as_body(mailbox, raw):
    with pytest.raises(ProviderError) as caught:
        provider(ScriptedTransport(page(row(raw=raw)))).list_messages(mailbox)
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"


def test_repeated_page_is_rejected_instead_of_polling_forever(mailbox):
    transport = ScriptedTransport(page(row(1), count=3), page(row(1), count=0))
    with pytest.raises(ProviderError) as caught:
        provider(transport).list_messages(mailbox)
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE"
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "status,code,uncertain",
    [
        (401, "INVALID_CREDENTIAL", False),
        (403, "PROVIDER_ACCESS_DENIED", False),
        (429, "PROVIDER_RATE_LIMITED", False),
        (503, "PROVIDER_UNAVAILABLE", True),
        (302, "PROVIDER_INVALID_RESPONSE", True),
    ],
)
def test_create_errors_are_classified_without_switching_modes(status, code, uncertain):
    transport = ScriptedTransport(Response(status))
    with pytest.raises(ProviderError) as caught:
        provider(transport, admin_password=ADMIN).create_mailbox(3600, "create")
    assert caught.value.code == code and caught.value.uncertain is uncertain
    assert len(transport.calls) == 1 and transport.calls[0]["url"].endswith("/admin/new_address")
    assert ADMIN not in str(caught.value)


def test_turnstile_failure_is_explicit_without_automatic_retries():
    blocked = Response(400)
    blocked.text = "Turnstile check failed"
    transport = ScriptedTransport(blocked)
    with pytest.raises(ProviderError) as caught:
        provider(transport).create_mailbox(3600, "create")
    assert caught.value.code == "CAPTCHA_REQUIRED" and not caught.value.uncertain
    assert len(transport.calls) == 1


@pytest.mark.parametrize("creating", [True, False])
def test_transport_failure_is_uncertain_only_during_creation(mailbox, creating):
    transport = ScriptedTransport(RequestException(f"secret {TOKEN} {ADMIN}"))
    adapter = provider(transport)
    with pytest.raises(ProviderError) as caught:
        adapter.create_mailbox(3600, "create") if creating else adapter.list_messages(mailbox)
    assert caught.value.code == "PROVIDER_UNAVAILABLE" and caught.value.uncertain is creating
    assert TOKEN not in str(caught.value) and ADMIN not in str(caught.value)


@pytest.mark.parametrize(
    "data",
    [
        {"address": EMAIL},
        {"address": "invalid", "jwt": TOKEN},
        {"address": EMAIL, "jwt": "secret\r\ninjected"},
        ValueError("secret response"),
    ],
)
def test_malformed_creation_response_preserves_uncertain_outcome(data):
    with pytest.raises(ProviderError) as caught:
        provider(ScriptedTransport(Response(200, data))).create_mailbox(3600, "create")
    assert caught.value.code == "PROVIDER_INVALID_RESPONSE" and caught.value.uncertain
    assert "secret" not in str(caught.value)


def test_configured_transport_and_capability_failures(mailbox, monkeypatch):
    transport = ScriptedTransport(created(), page())
    monkeypatch.setattr("app.providers.cloudflare_temp_email.requests.Session", transport)
    adapter = CloudflareTempEmailProvider(base_url=BASE, proxy="http://user:secret@localhost:7890", impersonate="chrome110")
    adapter.create_mailbox(3600, "create")
    assert adapter.list_messages(mailbox) == []
    assert transport.session_options == [{"proxy": "http://user:secret@localhost:7890", "impersonate": "chrome110"}] * 2
    for action in [
        lambda: adapter.send_message(mailbox, [EMAIL], "Subject", "Body", "send"),
        lambda: adapter.delete_mailbox(mailbox, "delete"),
    ]:
        with pytest.raises(ProviderError) as caught:
            action()
        assert caught.value.code == "CAPABILITY_UNSUPPORTED"
    with pytest.raises(ProviderError) as caught:
        adapter.list_messages(replace(mailbox, credential="bad\r\ntoken"))
    assert caught.value.code == "INVALID_CREDENTIAL"
    assert transport.sessions == 2


def test_api_restart_preserves_encrypted_binding_and_mime_body(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "mail.db"),
        api_token="integration-api-token-long-enough",
        encryption_key=Fernet.generate_key().decode(),
        providers={"cloudflare-temp-email": ProviderSettings(options={"base_url": BASE})},
    )
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    transport = ScriptedTransport(created(), page())
    with TestClient(create_app(settings, Registry([provider(transport)])), headers=headers) as client:
        r = client.post("/v1/mailboxes", json={"provider": "cloudflare-temp-email"}, headers={"Idempotency-Key": "create"})
        assert r.status_code == 202
        client.app.state.service.run_once()
        operation = client.get(f"/v1/operations/{r.json()['id']}").json()
        assert operation["status"] == "succeeded"
        mailbox_id = operation["result"]["mailbox_id"]
        with client.app.state.service.db.connect() as conn:
            encrypted = conn.execute("SELECT credential_encrypted FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()[0]
        assert TOKEN not in encrypted and Fernet(settings.encryption_key.encode()).decrypt(encrypted.encode()).decode() == TOKEN
    transport = ScriptedTransport(page(row()), page(row()))
    with TestClient(create_app(settings, Registry([provider(transport)])), headers=headers) as client:
        assert client.app.state.service.sync_mailbox(mailbox_id)
        assert client.app.state.service.sync_mailbox(mailbox_id)
        messages = client.get(f"/v1/mailboxes/{mailbox_id}/messages").json()["items"]
        assert len(messages) == 1
        r = client.get(f"/v1/mailboxes/{mailbox_id}/messages/{messages[0]['id']}")
        assert r.json()["text"].strip() == "你好，世界"
        assert r.json()["subject"] == "测试" and TOKEN not in r.text
        assert client.get(f"/v1/mailboxes/{mailbox_id}").json()["provider_id"] == "cloudflare-temp-email"
