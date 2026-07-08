from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

HandlingStatus = Literal["BOT_ACTIVE", "HANDOFF_PENDING", "HUMAN_ACTIVE"]
IssueStatus = Literal["NEW", "IN_PROGRESS", "CLOSED", "ESCALATED", "REOPENED"]


class IncomingMessage(BaseModel):
    event_id: str = Field(min_length=1)
    channel: Literal["synthetic", "telegram", "whatsapp"] = "synthetic"
    external_chat_id: str = Field(min_length=1)
    external_user_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=10_000)
    sender_name: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SupportReply(BaseModel):
    conversation_id: UUID
    answer: str
    confidence: float
    citations: list[str]
    escalated: bool
    handling_status: HandlingStatus
    issue_status: IssueStatus


class ConversationRecord(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    channel: str
    external_chat_id: str
    external_user_id: str
    status: HandlingStatus = "BOT_ACTIVE"
    issue_status: IssueStatus = "NEW"


class StoredMessage(BaseModel):
    conversation_id: UUID
    event_id: str
    sender_type: Literal["CUSTOMER", "BOT", "AGENT", "SYSTEM"]
    body: str


class ConversationStatusUpdate(BaseModel):
    status: HandlingStatus | None = None
    issue_status: IssueStatus | None = None
    reason: str | None = Field(default=None, max_length=1_000)
