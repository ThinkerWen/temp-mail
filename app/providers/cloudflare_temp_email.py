"""Receive-only adapter for a configured cloudflare_temp_email Worker instance."""

import hashlib
import math
import re
import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from email import policy
from email.errors import (
    CloseBoundaryNotFoundDefect,
    MissingHeaderBodySeparatorDefect,
    MultipartInvariantViolationDefect,
    StartBoundaryNotFoundDefect,
)
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlsplit

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from app.limits import validate_max_ttl_seconds
from app.providers.base import Capabilities, ProviderError, ProviderMailbox, ProviderMessage
from app.providers.message_text import html_to_text

FIRST_NAMES = (
    "alex",
    "alice",
    "amelia",
    "anna",
    "ben",
    "charlie",
    "chloe",
    "clara",
    "daniel",
    "ella",
    "emma",
    "ethan",
    "eva",
    "felix",
    "grace",
    "henry",
    "jack",
    "james",
    "julia",
    "leo",
    "liam",
    "lily",
    "lucas",
    "maya",
    "mia",
    "noah",
    "nora",
    "oliver",
    "oscar",
    "ruby",
    "sophie",
    "theo",
)
LAST_NAMES = (
    "adams",
    "allen",
    "baker",
    "bell",
    "bennett",
    "brooks",
    "brown",
    "carter",
    "clark",
    "cole",
    "collins",
    "cooper",
    "davis",
    "ellis",
    "evans",
    "foster",
    "gray",
    "green",
    "hall",
    "harris",
    "hayes",
    "hill",
    "lane",
    "lee",
    "miller",
    "moore",
    "parker",
    "reed",
    "scott",
    "stone",
    "walker",
    "wood",
)
WORDS = tuple("river meadow cedar maple willow ocean forest cloud breeze sunrise sunset autumn spring summer winter silver".split())


def _random_name() -> str:
    separator = secrets.choice(("", ".", "_"))
    style = secrets.randbelow(4)
    if style == 0:
        parts = [secrets.choice(FIRST_NAMES)]
    elif style == 1:
        parts = [secrets.choice(FIRST_NAMES), secrets.choice(LAST_NAMES)]
    elif style == 2:
        parts = [secrets.choice(FIRST_NAMES), secrets.choice(WORDS)]
    else:
        first = secrets.choice(WORDS)
        parts = [first, secrets.choice(tuple(word for word in WORDS if word != first))]
    suffix = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
    return separator.join(parts) + suffix


class CloudflareTempEmailProvider:
    id = "cloudflare-temp-email"
    capabilities = Capabilities(receive=True, send=False, delete=False, max_ttl_seconds=86400)

    def __init__(
        self,
        *,
        base_url: str = "https://your-worker.example.invalid",
        timeout_seconds: float = 15,
        impersonate: str = "chrome110",
        proxy: str | None = None,
        max_ttl_seconds: int = 86400,
        domain: str | None = None,
        admin_password: str | None = None,
        site_password: str | None = None,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    ):
        validate_max_ttl_seconds(max_ttl_seconds)
        self.capabilities = replace(type(self).capabilities, max_ttl_seconds=max_ttl_seconds)
        try:
            parsed = urlsplit(base_url)
            valid_url = (
                isinstance(base_url, str)
                and parsed.scheme in {"http", "https"}
                and parsed.hostname
                and (parsed.port is None or parsed.port > 0)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() for char in base_url)
            )
        except (ValueError, TypeError, AttributeError):
            valid_url = False
        if not valid_url:
            raise ValueError("Cloudflare Temp Email URL must be an HTTP(S) origin without credentials, query or fragment") from None
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Cloudflare Temp Email timeout must be a finite positive number")
        if proxy is not None and not isinstance(proxy, str):
            raise ValueError("Cloudflare Temp Email proxy must be a valid HTTP(S) or SOCKS proxy URL")
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
                raise ValueError("Cloudflare Temp Email proxy must be a valid HTTP(S) or SOCKS proxy URL") from None
        if domain is not None:
            if not isinstance(domain, str):
                raise ValueError("Cloudflare Temp Email domain must be a domain name")
            domain = domain.strip() or None
        if domain is not None and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain) is None:
            raise ValueError("Cloudflare Temp Email domain must be a domain name")
        for password in (admin_password, site_password):
            if password is not None and (not isinstance(password, str) or any(ord(char) < 32 or ord(char) == 127 for char in password)):
                raise ValueError("Cloudflare Temp Email passwords must not contain control characters")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.impersonate = impersonate
        self.domain = domain
        self.admin_password = admin_password or None
        self.site_password = site_password or None
        self.session_factory = session_factory or (lambda: requests.Session(impersonate=self.impersonate, proxy=self.proxy))

    @staticmethod
    def _invalid_response() -> ProviderError:
        return ProviderError("PROVIDER_INVALID_RESPONSE", "Cloudflare Temp Email returned an invalid response")

    @staticmethod
    def _valid_email(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", value) is not None

    @staticmethod
    def _valid_token(value: Any) -> bool:
        return isinstance(value, str) and bool(value) and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)

    def _check_configured(self) -> None:
        if urlsplit(self.base_url).hostname.endswith(".invalid"):
            raise ProviderError("PROVIDER_NOT_CONFIGURED", "Set the Cloudflare Temp Email Worker base URL before using this provider")

    def _request(self, session: Any, method: str, path: str, *, token: str | None = None, admin: bool = False, **kwargs) -> Any:
        headers = {"Accept": "application/json"}
        if self.site_password is not None:
            headers["x-custom-auth"] = self.site_password
        if admin:
            headers["x-admin-auth"] = self.admin_password
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = session.request(
                method, f"{self.base_url}{path}", headers=headers, timeout=self.timeout_seconds, allow_redirects=False, **kwargs
            )
        except (RequestException, TimeoutError, ConnectionError):
            raise ProviderError("PROVIDER_UNAVAILABLE", "Cloudflare Temp Email request could not be completed") from None
        status = response.status_code
        if status in {400, 403}:
            text = getattr(response, "text", "")
            if isinstance(text, str) and any(
                marker in text.lower() for marker in ("turnstile", "captcha", "human verification", "人机验证")
            ):
                raise ProviderError("CAPTCHA_REQUIRED", "Cloudflare Temp Email requires human verification")
        if status == 401:
            raise ProviderError("INVALID_CREDENTIAL", "Cloudflare Temp Email rejected the credentials")
        if status == 403:
            raise ProviderError("PROVIDER_ACCESS_DENIED", "Cloudflare Temp Email denied access")
        if status == 429:
            raise ProviderError("PROVIDER_RATE_LIMITED", "Cloudflare Temp Email request limit was reached")
        if status in {404, 410}:
            code = "MESSAGE_NOT_FOUND" if path.startswith("/api/mail/") else "MAILBOX_NOT_FOUND"
            raise ProviderError(code, "The requested Cloudflare Temp Email resource is no longer available")
        if status == 408 or status >= 500:
            raise ProviderError("PROVIDER_UNAVAILABLE", "Cloudflare Temp Email is temporarily unavailable")
        if 400 <= status < 500:
            raise ProviderError("PROVIDER_REQUEST_REJECTED", "Cloudflare Temp Email rejected the request")
        if not 200 <= status < 300:
            raise self._invalid_response()
        try:
            return response.json()
        except (ValueError, TypeError):
            raise self._invalid_response() from None

    def create_mailbox(self, ttl_seconds: int, request_id: str) -> ProviderMailbox:
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= self.capabilities.max_ttl_seconds:
            raise ProviderError("TTL_UNSUPPORTED", "Requested mailbox lifetime is unsupported")
        self._check_configured()
        admin = self.admin_password is not None
        payload = {}
        if admin:
            payload = {"name": _random_name(), "enablePrefix": False}
        if self.domain is not None:
            payload["domain"] = self.domain
        try:
            with self.session_factory() as session:
                data = self._request(session, "POST", "/admin/new_address" if admin else "/api/new_address", admin=admin, json=payload)
            if not isinstance(data, dict) or not self._valid_email(data.get("address")) or not self._valid_token(data.get("jwt")):
                raise self._invalid_response()
        except ProviderError as error:
            error.uncertain = error.code in {"PROVIDER_UNAVAILABLE", "PROVIDER_INVALID_RESPONSE"}
            raise
        upstream_id = hashlib.sha256(data["jwt"].encode()).hexdigest()
        # The instance owner controls upstream cleanup; the gateway applies its configured local TTL.
        return ProviderMailbox(upstream_id, data["address"], data["jwt"])

    def _received_at(self, value: Any, date_header: Any) -> str:
        try:
            if value is None:
                timestamp = parsedate_to_datetime(str(date_header))
            elif isinstance(value, str) and value.strip():
                timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                raise ValueError
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            return timestamp.astimezone(UTC).isoformat()
        except (ValueError, TypeError, OverflowError, OSError):
            raise self._invalid_response() from None

    def _parse_message(self, mailbox: ProviderMailbox, record: dict, message_id: str) -> ProviderMessage:
        if record.get("address") != mailbox.email or not isinstance(record.get("raw"), str) or not record["raw"].strip():
            raise self._invalid_response()
        try:
            parsed = BytesParser(policy=policy.default).parsebytes(record["raw"].encode("utf-8"))
            structural_defects = (
                MissingHeaderBodySeparatorDefect,
                StartBoundaryNotFoundDefect,
                CloseBoundaryNotFoundDefect,
                MultipartInvariantViolationDefect,
            )
            if not parsed.keys() or any(isinstance(defect, structural_defects) for part in parsed.walk() for defect in part.defects):
                raise self._invalid_response()
            body = parsed.get_body(preferencelist=("plain", "html"))
            if body is None:
                text = ""
            else:
                try:
                    text = body.get_content()
                except LookupError:
                    text = body.get_payload(decode=True).decode("utf-8", errors="replace")
                if body.get_content_type() == "text/html":
                    text = html_to_text(text)
            sender = parsed.get("From", record.get("source", ""))
            if not isinstance(sender, str) or not isinstance(text, str):
                raise self._invalid_response()
            # D1's envelope address also covers BCC deliveries, whose MIME To header may name someone else.
            return ProviderMessage(
                message_id,
                str(sender),
                [mailbox.email],
                str(parsed.get("Subject", "")),
                text,
                self._received_at(record.get("created_at"), parsed.get("Date")),
            )
        except (ValueError, TypeError, LookupError):
            raise self._invalid_response() from None

    def list_messages(self, mailbox: ProviderMailbox) -> list[ProviderMessage]:
        self._check_configured()
        if not self._valid_token(mailbox.credential) or not self._valid_email(mailbox.email):
            raise ProviderError("INVALID_CREDENTIAL", "Invalid Cloudflare Temp Email mailbox credentials")
        messages = []
        seen: set[str] = set()
        offset, total = 0, None
        with self.session_factory() as session:
            for _ in range(1000):
                data = self._request(session, "GET", "/api/mails", token=mailbox.credential, params={"limit": 100, "offset": offset})
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise self._invalid_response()
                count = data.get("count")
                if type(count) is not int or not 0 <= count <= 100000:
                    raise self._invalid_response()
                if total is None:
                    total = count
                records = data["results"]
                if not records:
                    return messages
                if len(records) > 100:
                    raise self._invalid_response()
                previous_count = len(seen)
                for record in records:
                    if not isinstance(record, dict) or record.get("address") != mailbox.email:
                        raise self._invalid_response()
                    identifier = record.get("id")
                    if type(identifier) not in {int, str} or not str(identifier).strip():
                        raise self._invalid_response()
                    message_id = str(identifier)
                    if message_id in {".", ".."}:
                        raise self._invalid_response()
                    if message_id in seen:
                        continue
                    seen.add(message_id)
                    if record.get("raw") is None:
                        try:
                            detail = self._request(session, "GET", f"/api/mail/{quote(message_id, safe='')}", token=mailbox.credential)
                        except ProviderError as error:
                            if error.code == "MESSAGE_NOT_FOUND":
                                continue
                            raise
                        if detail is None:
                            continue
                        if not isinstance(detail, dict) or str(detail.get("id")) != message_id:
                            raise self._invalid_response()
                        record = detail
                    messages.append(self._parse_message(mailbox, record, message_id))
                if len(seen) == previous_count:
                    raise self._invalid_response()
                offset += len(records)
                if offset >= total:
                    return messages
        raise self._invalid_response()

    def send_message(self, mailbox: ProviderMailbox, recipients: list[str], subject: str, text: str, request_id: str) -> str:
        raise ProviderError("CAPABILITY_UNSUPPORTED", "Cloudflare Temp Email sending is not supported by this integration")

    def delete_mailbox(self, mailbox: ProviderMailbox, request_id: str) -> None:
        raise ProviderError("CAPABILITY_UNSUPPORTED", "Cloudflare Temp Email upstream deletion is not supported by this integration")
