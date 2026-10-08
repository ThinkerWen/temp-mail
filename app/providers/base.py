"""Provider contracts; vendor objects and credentials stay behind this boundary."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Capabilities:
    receive: bool = True
    send: bool = False
    delete: bool = True
    attachments: bool = False
    webhook: bool = False
    custom_local_part: bool = False
    max_ttl_seconds: int = 86400
    destructive_receive: bool = False


@dataclass(frozen=True)
class ProviderMailbox:
    upstream_id: str
    email: str
    credential: str
    expires_at: str | None = None


@dataclass(frozen=True)
class ProviderMessage:
    upstream_id: str
    sender: str
    recipients: list[str]
    subject: str
    text: str
    received_at: str


class ProviderError(Exception):
    def __init__(self, code: str, message: str, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain


class Provider(Protocol):
    id: str
    capabilities: Capabilities

    def create_mailbox(self, ttl_seconds: int, request_id: str) -> ProviderMailbox: ...

    def list_messages(self, mailbox: ProviderMailbox) -> list[ProviderMessage]: ...

    def send_message(self, mailbox: ProviderMailbox, recipients: list[str], subject: str, text: str, request_id: str) -> str: ...

    def delete_mailbox(self, mailbox: ProviderMailbox, request_id: str) -> None: ...


class DestructiveReceiveProvider(Provider, Protocol):
    """Raw responses must be durably staged before normalization for consuming APIs."""

    def fetch_messages(self, mailbox: ProviderMailbox) -> dict: ...

    def parse_messages(self, mailbox: ProviderMailbox, payload: dict, batch_id: str) -> list[ProviderMessage]: ...
