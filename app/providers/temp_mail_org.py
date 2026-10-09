"""Receive-only adapter for the public temp-mail.org website's mailbox API."""

import hashlib
import math
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from app.limits import validate_max_ttl_seconds
from app.providers.base import Capabilities, ProviderError, ProviderMailbox, ProviderMessage
from app.providers.message_text import html_to_text


class TempMailOrgProvider:
    id = "temp-mail-org"
    # This is the local retention limit, not a guarantee of upstream mailbox lifetime.
    capabilities = Capabilities(receive=True, send=False, delete=False, attachments=False, max_ttl_seconds=86400)

    def __init__(
        self,
        *,
        base_url: str = "https://web2.temp-mail.org",
        timeout_seconds: float = 15,
        impersonate: str = "chrome110",
        proxy: str | None = None,
        max_ttl_seconds: int = 86400,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    ):
        validate_max_ttl_seconds(max_ttl_seconds)
        self.capabilities = replace(type(self).capabilities, max_ttl_seconds=max_ttl_seconds)
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("Temp Mail API URL must be an HTTP(S) URL without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Temp Mail API URL must not contain a query or fragment")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Temp Mail timeout must be a finite positive number")
        self.proxy = proxy.strip() or None if proxy is not None else None
        if self.proxy is not None:
            try:
                proxy_url = urlsplit(self.proxy)
                valid_proxy = (
                    proxy_url.scheme in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
                    and proxy_url.hostname
                    and (proxy_url.port is None or proxy_url.port > 0)
                    and proxy_url.path in {"", "/"}
                    and not proxy_url.query
                    and not proxy_url.fragment
                    and not any(char.isspace() for char in self.proxy)
                )
            except ValueError:
                valid_proxy = False
            if not valid_proxy:
                raise ValueError("Temp Mail proxy must be a valid HTTP(S) or SOCKS proxy URL") from None
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.impersonate = impersonate
        self.session_factory = session_factory or (lambda: requests.Session(impersonate=self.impersonate, proxy=self.proxy))

    @staticmethod
    def _invalid_response(uncertain: bool = False) -> ProviderError:
        return ProviderError("PROVIDER_INVALID_RESPONSE", "Temp Mail returned an invalid response", uncertain=uncertain)

    def _request(self, session: Any, method: str, path: str, token: str | None = None) -> dict:
        creating = method == "POST"
        headers = {"Accept": "application/json", "Content-Type": "application/json", "Origin": "https://temp-mail.org"}
        headers["Referer"] = "https://temp-mail.org/"
        if token is not None:
            if not isinstance(token, str) or not token or any(char in token for char in "\r\n"):
                raise ProviderError("INVALID_CREDENTIAL", "Invalid upstream mailbox credentials")
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = session.request(
                method, f"{self.base_url}{path}", headers=headers, timeout=self.timeout_seconds, allow_redirects=False
            )
        except (RequestException, TimeoutError, ConnectionError):
            raise ProviderError("PROVIDER_UNAVAILABLE", "Temp Mail request could not be completed", uncertain=creating) from None
        status = response.status_code
        if status == 401:
            raise ProviderError("INVALID_CREDENTIAL", "Temp Mail rejected the mailbox credentials")
        if status == 403:
            raise ProviderError("PROVIDER_ACCESS_DENIED", "Temp Mail denied access to its API")
        if status == 429:
            raise ProviderError("PROVIDER_RATE_LIMITED", "Temp Mail request limit was reached")
        if status in {404, 410}:
            code = "MESSAGE_NOT_FOUND" if path.startswith("/messages/") else "MAILBOX_NOT_FOUND"
            raise ProviderError(code, "The requested resource is no longer available at Temp Mail")
        if status == 408 or status >= 500:
            raise ProviderError("PROVIDER_UNAVAILABLE", "Temp Mail is temporarily unavailable", uncertain=creating)
        if 400 <= status < 500:
            raise ProviderError("PROVIDER_REQUEST_REJECTED", "Temp Mail rejected the request")
        if not 200 <= status < 300:
            raise self._invalid_response(uncertain=creating)
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise self._invalid_response(uncertain=creating) from None
        if not isinstance(data, dict):
            raise self._invalid_response(uncertain=creating)
        return data

    def create_mailbox(self, ttl_seconds: int, request_id: str) -> ProviderMailbox:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 0 < ttl_seconds <= self.capabilities.max_ttl_seconds:
            raise ProviderError("TTL_UNSUPPORTED", "Requested mailbox lifetime is unsupported")
        # No upstream idempotency contract exists: the worker must not replay an uncertain creation.
        with self.session_factory() as session:
            data = self._request(session, "POST", "/mailbox")
        token, email = data.get("token"), data.get("mailbox")
        if not isinstance(token, str) or not token.strip() or any(char in token for char in "\r\n"):
            raise self._invalid_response(uncertain=True)
        if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", email):
            raise self._invalid_response(uncertain=True)
        # A token identifies a mailbox lifecycle even if an address is later recycled.
        return ProviderMailbox(hashlib.sha256(token.encode()).hexdigest(), email, token)

    def _received_at(self, value: Any) -> str:
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return datetime.fromtimestamp(value, UTC).isoformat()
            if isinstance(value, str):
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if result.tzinfo is not None:
                    return result.astimezone(UTC).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
        raise self._invalid_response()

    def _message(self, data: dict, message_id: str, email: str) -> ProviderMessage:
        if data.get("_id") != message_id or ("mailbox" in data and data["mailbox"] != email):
            raise self._invalid_response()
        sender, subject = data.get("from"), data.get("subject", "")
        if not isinstance(sender, str) or (subject is not None and not isinstance(subject, str)):
            raise self._invalid_response()
        body_text, body_html = data.get("bodyText"), data.get("bodyHtml")
        if body_text is not None and not isinstance(body_text, str):
            raise self._invalid_response()
        if body_html is not None and not isinstance(body_html, str):
            raise self._invalid_response()
        if body_text is not None and (body_text.strip() or body_html is None):
            text = body_text
        elif body_html is not None:
            text = html_to_text(body_html)
        else:
            raise self._invalid_response()
        return ProviderMessage(message_id, sender, [email], subject or "", text, self._received_at(data.get("receivedAt")))

    def list_messages(self, mailbox: ProviderMailbox) -> list[ProviderMessage]:
        messages = []
        with self.session_factory() as session:
            data = self._request(session, "GET", "/messages", mailbox.credential)
            if data.get("mailbox") != mailbox.email or not isinstance(data.get("messages"), list):
                raise self._invalid_response()
            seen = set()
            for item in data["messages"]:
                if not isinstance(item, dict) or not isinstance(item.get("_id"), str) or not item["_id"]:
                    raise self._invalid_response()
                message_id = item["_id"]
                if message_id in {".", ".."}:
                    raise self._invalid_response()
                if message_id in seen:
                    continue
                seen.add(message_id)
                try:
                    detail = self._request(session, "GET", f"/messages/{quote(message_id, safe='')}", mailbox.credential)
                except ProviderError as error:
                    if error.code == "MESSAGE_NOT_FOUND":
                        continue
                    raise
                messages.append(self._message(detail, message_id, mailbox.email))
        return messages

    def send_message(self, mailbox: ProviderMailbox, recipients: list[str], subject: str, text: str, request_id: str) -> str:
        raise ProviderError("CAPABILITY_UNSUPPORTED", "Temp Mail does not support sending through this integration")

    def delete_mailbox(self, mailbox: ProviderMailbox, request_id: str) -> None:
        raise ProviderError("CAPABILITY_UNSUPPORTED", "Temp Mail does not expose upstream mailbox deletion through this integration")
