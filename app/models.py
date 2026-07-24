from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

ConversationState = Literal["BOT_ACTIVE", "HUMAN_REQUESTED", "HUMAN_ACTIVE"]


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
    low_confidence: bool
    state: ConversationState


class ConversationRecord(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    channel: str
    external_chat_id: str
    external_user_id: str
    state: ConversationState = "BOT_ACTIVE"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StoredMessage(BaseModel):
    conversation_id: UUID
    event_id: str
    sender_type: Literal["CUSTOMER", "BOT", "AGENT", "SYSTEM"]
    body: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConversationPromptMetadata(BaseModel):
    is_first_customer_message: bool
    customer_name: str | None = None
    minutes_since_last_customer_message: int | None = None
    should_greet_customer: bool
    greeting_reason: str


class ConversationStateUpdate(BaseModel):
    state: ConversationState
    reason: str | None = Field(default=None, max_length=1_000)
