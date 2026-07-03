from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


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


class ConversationRecord(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    channel: str
    external_chat_id: str
    external_user_id: str
    status: str = "BOT_ACTIVE"


class StoredMessage(BaseModel):
    conversation_id: UUID
    event_id: str
    sender_type: Literal["CUSTOMER", "BOT", "AGENT", "SYSTEM"]
    body: str
