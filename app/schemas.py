from typing import Annotated, Literal

from pydantic import BaseModel, Field

Capability = Literal["receive", "send", "delete", "attachments", "webhook", "custom_local_part"]
EmailAddress = Annotated[str, Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")]


class CreateMailbox(BaseModel):
    model_config = {"extra": "forbid"}
    provider: str = Field(default="auto", min_length=1, max_length=100)
    required_capabilities: list[Capability] = Field(default_factory=lambda: ["receive"], min_length=1)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class SendMessage(BaseModel):
    model_config = {"extra": "forbid"}
    recipients: list[EmailAddress] = Field(min_length=1, max_length=50)
    subject: str = Field(default="", max_length=998, pattern=r"^[^\r\n]*$")
    text: str = Field(min_length=1, max_length=1_000_000)


class OperationView(BaseModel):
    id: str
    kind: Literal["create", "send", "delete"]
    status: Literal["pending", "running", "succeeded", "failed", "unknown"]
    provider_id: str
    mailbox_id: str | None
    result: dict | None
    error_code: str | None
    created_at: str
    updated_at: str


class MailboxView(BaseModel):
    id: str
    email: str
    provider_id: str
    capabilities: dict
    status: Literal["active", "expired", "deleted"]
    created_at: str
    expires_at: str
    last_synced_at: str | None
    last_sync_error_code: str | None


class MessageSummary(BaseModel):
    id: str
    mailbox_id: str
    sender: str
    recipients: list[str]
    subject: str
    received_at: str


class MessageView(MessageSummary):
    text: str


class MailboxPage(BaseModel):
    items: list[MailboxView]
    limit: int
    offset: int


class MessagePage(BaseModel):
    items: list[MessageSummary]
    limit: int
    offset: int
    last_synced_at: str | None
