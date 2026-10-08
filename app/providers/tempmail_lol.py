"""TempMail.lol v2 adapter: fetching an inbox consumes its upstream messages."""

import hashlib
import math
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from app.providers.base import Capabilities, ProviderError, ProviderMailbox, ProviderMessage
from app.providers.message_text import html_to_text


class TempMailLolProvider:
    id = "tempmail-lol"
    capabilities = Capabilities(receive=True, send=False, delete=False, max_ttl_seconds=3600, destructive_receive=True)

    def __init__(
        self,
        *,
        base_url: str = "https://api.tempmail.lol/v2",
        timeout_seconds: float = 15,
        impersonate: str = "chrome110",
        proxy: str | None = None,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    ):
        try:
            parsed = urlsplit(base_url)
            valid_url = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("TempMail.lol API URL must be HTTP(S), without credentials, query or fragment") from None
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("TempMail.lol timeout must be a finite positive number")
        self.proxy = proxy.strip() or None if proxy is not None else None
        if self.proxy is not None:
            try:
                parsed_proxy = urlsplit(self.proxy)
                valid_proxy = (
                    parsed_proxy.scheme in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
                    and parsed_proxy.hostname
                    and (parsed_proxy.port is None or parsed_proxy.port > 0)
                    and parsed_proxy.path in {"", "/"}
                    and not parsed_proxy.query
                    and not parsed_proxy.fragment
                    and not any(char.isspace() for char in self.proxy)
                )
            except ValueError:
                valid_proxy = False
            if not valid_proxy:
                raise ValueError("TempMail.lol proxy must be a valid HTTP(S) or SOCKS proxy URL") from None
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.impersonate = impersonate
        self.session_factory = session_factory or (lambda: requests.Session(impersonate=self.impersonate, proxy=self.proxy))

    @staticmethod
    def _invalid_response(uncertain: bool = False) -> ProviderError:
        return ProviderError("PROVIDER_INVALID_RESPONSE", "TempMail.lol returned an invalid response", uncertain=uncertain)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Accept": "application/json", "Content-Type": "application/json", "Origin": "https://tempmail.lol"}
        headers["Referer"] = "https://tempmail.lol/"
        try:
            with self.session_factory() as session:
                response = session.request(
                    method, f"{self.base_url}{path}", headers=headers, timeout=self.timeout_seconds, allow_redirects=False, **kwargs
                )
        except (RequestException, TimeoutError, ConnectionError):
            # Both creating and GET-fetching have side effects. Never replay either automatically.
            raise ProviderError("PROVIDER_UNAVAILABLE", "TempMail.lol request could not be completed", uncertain=True) from None
        status = response.status_code
        if status == 401:
            raise ProviderError("INVALID_CREDENTIAL", "TempMail.lol rejected the inbox credentials")
        if status == 403:
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and payload.get("captcha_required") is True:
                raise ProviderError("CAPTCHA_REQUIRED", "TempMail.lol requires a CAPTCHA")
            raise ProviderError("PROVIDER_ACCESS_DENIED", "TempMail.lol denied access")
        if status == 429:
            raise ProviderError("PROVIDER_RATE_LIMITED", "TempMail.lol request limit was reached")
        if status in {404, 410}:
            code = "MAILBOX_EXPIRED" if status == 410 else "MAILBOX_NOT_FOUND"
            raise ProviderError(code, "The inbox is no longer available at TempMail.lol")
        if status == 408 or status >= 500:
            raise ProviderError("PROVIDER_UNAVAILABLE", "TempMail.lol is temporarily unavailable", uncertain=True)
        if 400 <= status < 500:
            raise ProviderError("PROVIDER_REQUEST_REJECTED", "TempMail.lol rejected the request")
        if not 200 <= status < 300:
            raise self._invalid_response(uncertain=True)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise self._invalid_response(uncertain=True) from None
        if not isinstance(payload, dict):
            raise self._invalid_response(uncertain=True)
        return payload

    @staticmethod
    def _check_envelope(payload: dict) -> None:
        if payload.get("captcha_required") is True:
            raise ProviderError("CAPTCHA_REQUIRED", "TempMail.lol requires a CAPTCHA")
        if payload.get("expired") is True:
            raise ProviderError("MAILBOX_EXPIRED", "The TempMail.lol inbox has expired")
        if payload.get("error"):
            raise ProviderError("PROVIDER_REQUEST_REJECTED", "TempMail.lol rejected the request")

    def create_mailbox(self, ttl_seconds: int, request_id: str) -> ProviderMailbox:
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= self.capabilities.max_ttl_seconds:
            raise ProviderError("TTL_UNSUPPORTED", "Requested inbox lifetime is unsupported")
        expires_at = (datetime.now(UTC) + timedelta(seconds=3600)).isoformat()
        payload = self._request("POST", "/inbox/create", json={"domain": None, "captcha": None})
        self._check_envelope(payload)
        email, token = payload.get("address"), payload.get("token")
        if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", email):
            raise self._invalid_response(uncertain=True)
        if not isinstance(token, str) or not token.strip() or any(char in token for char in "\r\n"):
            raise self._invalid_response(uncertain=True)
        return ProviderMailbox(hashlib.sha256(token.encode()).hexdigest(), email, token, expires_at)

    def fetch_messages(self, mailbox: ProviderMailbox) -> dict:
        """Consume upstream mail and return JSON for durable staging before schema validation."""
        token = mailbox.credential
        if not isinstance(token, str) or not token.strip() or any(char in token for char in "\r\n"):
            raise ProviderError("INVALID_CREDENTIAL", "Invalid TempMail.lol inbox credentials")
        payload = self._request("GET", "/inbox", params={"token": token})
        # Explicit failures without mail can be retried; preserve any returned mail before interpreting the envelope.
        if not isinstance(payload.get("emails"), list) or not payload["emails"]:
            self._check_envelope(payload)
        return payload

    def _received_at(self, value: Any) -> str:
        try:
            if type(value) in {int, float} and math.isfinite(value):
                return datetime.fromtimestamp(value / 1000, UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
        raise self._invalid_response()

    def parse_messages(self, mailbox: ProviderMailbox, payload: dict, batch_id: str) -> list[ProviderMessage]:
        """Parse a persisted batch; retries must retain batch_id to preserve fallback identities."""
        if not isinstance(payload, dict):
            raise self._invalid_response()
        self._check_envelope(payload)
        if "expired" in payload and type(payload["expired"]) is not bool:
            raise self._invalid_response()
        if not isinstance(payload.get("emails"), list) or not isinstance(batch_id, str) or not batch_id:
            raise self._invalid_response()
        messages = []
        for index, item in enumerate(payload["emails"]):
            if not isinstance(item, dict):
                raise self._invalid_response()
            sender, recipient, subject = item.get("from"), item.get("to"), item.get("subject", "")
            if not isinstance(sender, str) or recipient != mailbox.email or not isinstance(subject, str):
                raise self._invalid_response()
            body, html = item.get("body"), item.get("html")
            if (body is not None and not isinstance(body, str)) or (html is not None and not isinstance(html, str)):
                raise self._invalid_response()
            if body is not None and (body.strip() or html is None):
                text = body
            elif html is not None:
                text = html_to_text(html)
            else:
                raise self._invalid_response()
            identifiers = (item.get("id"), item.get("_id"))
            message_id = next((value for value in identifiers if isinstance(value, str) and value.strip()), f"{batch_id}:{index}")
            messages.append(ProviderMessage(message_id, sender, [recipient], subject, text, self._received_at(item.get("date"))))
        return messages

    def list_messages(self, mailbox: ProviderMailbox) -> list[ProviderMessage]:
        """Direct callers must persist results themselves; the gateway stages fetch_messages first."""
        return self.parse_messages(mailbox, self.fetch_messages(mailbox), uuid4().hex)

    def send_message(self, mailbox: ProviderMailbox, recipients: list[str], subject: str, text: str, request_id: str) -> str:
        raise ProviderError("CAPABILITY_UNSUPPORTED", "TempMail.lol does not support sending through this integration")

    def delete_mailbox(self, mailbox: ProviderMailbox, request_id: str) -> None:
        raise ProviderError("CAPABILITY_UNSUPPORTED", "TempMail.lol v2 inbox deletion is not supported by this integration")
