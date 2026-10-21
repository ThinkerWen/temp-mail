"""Receive-only adapter for 22.do's anonymous website mailbox flow."""

import json
import math
import re
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import getaddresses
from html.parser import HTMLParser
from threading import Lock
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit
from uuid import uuid4

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from app.limits import validate_max_ttl_seconds
from app.providers.base import Capabilities, ProviderError, ProviderMailbox, ProviderMessage
from app.providers.message_text import html_to_text


class _IframeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "iframe":
            self.sources.extend(value for name, value in attrs if name == "src" and value)


class TwentyTwoDoProvider:
    id = "22-do"
    capabilities = Capabilities(receive=True, send=False, delete=False, attachments=False, max_ttl_seconds=86400)

    def __init__(
        self,
        *,
        base_url: str = "https://22.do",
        timeout_seconds: float = 15,
        impersonate: str = "chrome110",
        proxy: str | None = None,
        max_ttl_seconds: int = 86400,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
    ):
        validate_max_ttl_seconds(max_ttl_seconds)
        self.capabilities = replace(type(self).capabilities, max_ttl_seconds=max_ttl_seconds)
        try:
            parsed = urlsplit(base_url)
            valid_url = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and (parsed.port is None or parsed.port > 0)
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() for char in base_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("22.do URL must be an HTTP(S) origin without credentials, query or fragment") from None
        if type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("22.do timeout must be a finite positive number")
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
                raise ValueError("22.do proxy must be a valid HTTP(S) or SOCKS proxy URL") from None
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.impersonate = impersonate
        self.session_factory = session_factory or (lambda: requests.Session(impersonate=self.impersonate, proxy=self.proxy))
        self._tokens: dict[str, tuple[str, float]] = {}
        self._token_lock = Lock()

    @staticmethod
    def _invalid_response() -> ProviderError:
        return ProviderError("PROVIDER_INVALID_RESPONSE", "22.do returned an invalid response")

    @staticmethod
    def _valid_token(token: Any) -> bool:
        return isinstance(token, str) and bool(token.strip()) and not any(char.isspace() for char in token)

    @staticmethod
    def _valid_email(email: Any) -> bool:
        return isinstance(email, str) and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", email) is not None

    def _request(self, session: Any, method: str, path: str, *, token: str | None = None, payload: dict | None = None) -> Any:
        headers = {"Accept": "application/json" if payload is not None else "text/html", "Referer": f"{self.base_url}/inbox"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        options = {}
        if payload is not None:
            headers.update({"Content-Type": "application/json", "Origin": self.base_url})
            options["json"] = payload
        try:
            response = session.request(
                method, f"{self.base_url}{path}", headers=headers, timeout=self.timeout_seconds, allow_redirects=False, **options
            )
        except (RequestException, TimeoutError, ConnectionError):
            raise ProviderError("PROVIDER_UNAVAILABLE", "22.do request could not be completed") from None
        status = response.status_code
        if status == 401:
            raise ProviderError("INVALID_CREDENTIAL", "22.do rejected the mailbox credentials")
        if status == 403:
            raise ProviderError("PROVIDER_ACCESS_DENIED", "22.do denied access")
        if status == 429:
            raise ProviderError("PROVIDER_RATE_LIMITED", "22.do request limit was reached")
        if status in {404, 410}:
            code = "MESSAGE_NOT_FOUND" if path.startswith(("/content/", "/view/")) else "MAILBOX_NOT_FOUND"
            raise ProviderError(code, "The requested resource is no longer available at 22.do")
        if status == 408 or status >= 500:
            raise ProviderError("PROVIDER_UNAVAILABLE", "22.do is temporarily unavailable")
        if 400 <= status < 500:
            raise ProviderError("PROVIDER_REQUEST_REJECTED", "22.do rejected the request")
        if not 200 <= status < 300:
            raise self._invalid_response()
        if payload is None:
            if not isinstance(response.text, str):
                raise self._invalid_response()
            headers = getattr(response, "headers", {}) or {}
            challenge_header = any(key.lower() == "cf-mitigated" and str(value).lower() == "challenge" for key, value in headers.items())
            challenge_title = re.search(r"<title>\s*Just a moment(?:\.{3}|…)\s*</title>", response.text, re.IGNORECASE)
            challenge_page = challenge_title and any(
                marker in response.text.lower()
                for marker in (
                    "challenge-platform",
                    "challenges.cloudflare.com",
                    "verify you are human",
                    "enable javascript and cookies to continue",
                )
            )
            if challenge_header or challenge_page:
                raise ProviderError("PROVIDER_ACCESS_DENIED", "22.do requires a browser challenge")
            return response.text
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise self._invalid_response() from None
        if not isinstance(data, dict) or data.get("status") is not True or "data" not in data:
            raise self._invalid_response()
        return data["data"]

    def _timestamp(self, value: Any) -> float:
        try:
            if type(value) in {int, float} and math.isfinite(value):
                datetime.fromtimestamp(value, UTC)
                return float(value)
            if isinstance(value, str):
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if result.tzinfo is not None:
                    return result.timestamp()
        except (ValueError, OverflowError, OSError):
            pass
        raise self._invalid_response()

    def _apply_token(self, session: Any, email: str, uuid: str) -> tuple[str, float]:
        data = self._request(session, "POST", "/action/mailbox/applyToken", payload={"email": email, "uuid": uuid})
        if not isinstance(data, dict) or not self._valid_token(data.get("token")):
            raise self._invalid_response()
        expiry = self._timestamp(data.get("expireTime"))
        if expiry <= time.time():
            raise self._invalid_response()
        return data["token"], expiry

    def create_mailbox(self, ttl_seconds: int, request_id: str) -> ProviderMailbox:
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= self.capabilities.max_ttl_seconds:
            raise ProviderError("TTL_UNSUPPORTED", "Requested mailbox lifetime is unsupported")
        created_at = int(time.time())
        allocated = False
        try:
            with self.session_factory() as session:
                page = self._request(session, "GET", "/inbox")
                allocated = True
                address = re.search(r'\b(?:let|const|var)\s+email\s*=\s*("(?:[^"\\]|\\.)*")', page)
                lifetime = re.search(r"\b(?:let|const|var)\s+timeLeft\s*=\s*(\d+)\s*[;,]", page)
                if address is None or lifetime is None:
                    raise self._invalid_response()
                try:
                    email = json.loads(address.group(1))
                    seconds = int(lifetime.group(1))
                    expires_at = datetime.fromtimestamp(created_at + seconds, UTC).isoformat()
                except (ValueError, OverflowError, OSError):
                    raise self._invalid_response() from None
                if not self._valid_email(email) or seconds <= 0:
                    raise self._invalid_response()
                uuid = uuid4().hex
                token, token_expiry = self._apply_token(session, email, uuid)
        except ProviderError as error:
            # An explicit initial rejection allocates nothing; a later failure may strand an allocated address.
            error.uncertain = allocated or error.code in {"PROVIDER_UNAVAILABLE", "PROVIDER_INVALID_RESPONSE"}
            raise
        credential = json.dumps(
            {"email": email, "uuid": uuid, "token": token, "token_expires_at": token_expiry, "created_at": created_at},
            separators=(",", ":"),
        )
        return ProviderMailbox(uuid, email, credential, expires_at)

    def _credentials(self, mailbox: ProviderMailbox) -> dict:
        try:
            data = json.loads(mailbox.credential)
            if (
                not isinstance(data, dict)
                or data.get("email") != mailbox.email
                or not self._valid_email(mailbox.email)
                or not isinstance(data.get("uuid"), str)
                or re.fullmatch(r"[a-f0-9]{32}", data["uuid"]) is None
                or data["uuid"] != mailbox.upstream_id
                or not self._valid_token(data.get("token"))
            ):
                raise ValueError
            data["created_at"] = self._timestamp(data.get("created_at"))
            data["token_expires_at"] = self._timestamp(data.get("token_expires_at"))
            return data
        except (ValueError, TypeError, ProviderError):
            raise ProviderError("INVALID_CREDENTIAL", "Invalid upstream mailbox credentials") from None

    def _token(self, session: Any, data: dict, *, renew: bool = False) -> str:
        with self._token_lock:
            token, expiry = self._tokens.get(data["uuid"], (data["token"], data["token_expires_at"]))
        if renew or expiry <= time.time() + 30:
            # The service serializes work per mailbox. Unrelated mailboxes may renew concurrently.
            token, expiry = self._apply_token(session, data["email"], data["uuid"])
            now = time.time()
            with self._token_lock:
                # Refreshed credentials are process-local; the persisted UUID can always renew them after restart.
                self._tokens = {key: value for key, value in self._tokens.items() if value[1] > now}
                if len(self._tokens) >= 1024:
                    self._tokens.pop(next(iter(self._tokens)))
                self._tokens[data["uuid"]] = (token, expiry)
        return token

    def _body(self, session: Any, message_id: str) -> str:
        page = self._request(session, "GET", f"/content/{quote(message_id, safe='')}")
        parser = _IframeParser()
        parser.feed(page)
        parser.close()
        origin = urlsplit(self.base_url)
        paths = set()
        for source in parser.sources:
            try:
                url = urlsplit(urljoin(f"{self.base_url}/content/{quote(message_id, safe='')}", source))
                decoded_path = unquote(url.path)
                if (
                    (url.scheme, url.hostname, url.port or (443 if url.scheme == "https" else 80))
                    != (origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80))
                    or url.username is not None
                    or url.password is not None
                    or url.fragment
                    or url.query
                    or not url.path.startswith("/view/")
                    or not url.path.removeprefix("/view/")
                    or any(char.isspace() for char in source)
                    or "\\" in decoded_path
                    or any(part in {".", ".."} for part in decoded_path.split("/"))
                ):
                    continue
                paths.add(url.path)
            except ValueError:
                continue
        if len(paths) != 1:
            raise self._invalid_response()
        # The body iframe is public; never forward the mailbox's Bearer token to it.
        return html_to_text(self._request(session, "GET", paths.pop()))

    def list_messages(self, mailbox: ProviderMailbox) -> list[ProviderMessage]:
        credential = self._credentials(mailbox)
        messages = []
        with self.session_factory() as session:
            payload = {"email": mailbox.email, "lastime": int(credential["created_at"])}
            token = self._token(session, credential)
            try:
                items = self._request(session, "POST", "/action/mailbox/message", token=token, payload=payload)
            except ProviderError as error:
                if error.code != "INVALID_CREDENTIAL":
                    raise
                token = self._token(session, credential, renew=True)
                items = self._request(session, "POST", "/action/mailbox/message", token=token, payload=payload)
            if items is None or items is False:
                return []
            if not isinstance(items, list):
                raise self._invalid_response()
            seen = set()
            for item in items:
                if not isinstance(item, dict):
                    raise self._invalid_response()
                message_id = item.get("messageId")
                if type(message_id) is int:
                    message_id = str(message_id)
                if not isinstance(message_id, str) or not message_id or message_id in {".", ".."}:
                    raise self._invalid_response()
                received_at = self._timestamp(item.get("time"))
                if received_at < credential["created_at"]:
                    continue
                recipient = item.get("to")
                if isinstance(recipient, str):
                    recipient = [recipient]
                if not isinstance(recipient, list) or not recipient or not all(isinstance(value, str) for value in recipient):
                    raise self._invalid_response()
                recipients = [address for _, address in getaddresses(recipient)]
                if not recipients or not all(self._valid_email(address) for address in recipients):
                    raise self._invalid_response()
                if mailbox.email.casefold() not in {address.casefold() for address in recipients}:
                    continue
                if message_id in seen:
                    continue
                sender, subject = item.get("from"), item.get("subject", "")
                if not isinstance(sender, str) or not isinstance(subject, str):
                    raise self._invalid_response()
                seen.add(message_id)
                try:
                    body = self._body(session, message_id)
                except ProviderError as error:
                    if error.code == "MESSAGE_NOT_FOUND":
                        continue
                    raise
                messages.append(
                    ProviderMessage(message_id, sender, recipients, subject, body, datetime.fromtimestamp(received_at, UTC).isoformat())
                )
        return messages

    def send_message(self, mailbox: ProviderMailbox, recipients: list[str], subject: str, text: str, request_id: str) -> str:
        raise ProviderError("CAPABILITY_UNSUPPORTED", "22.do does not support sending through this integration")

    def delete_mailbox(self, mailbox: ProviderMailbox, request_id: str) -> None:
        raise ProviderError("CAPABILITY_UNSUPPORTED", "22.do does not expose upstream mailbox deletion through this integration")
