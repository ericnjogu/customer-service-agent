import re
from datetime import datetime
from uuid import UUID

from langchain_core.documents import Document

from app.models import ConversationRecord, IncomingMessage, StoredMessage

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "do",
    "for",
    "how",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "the",
    "to",
    "what",
    "you",
    "your",
}


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - STOP_WORDS


class MemoryConversationRepository:
    def __init__(self) -> None:
        self.conversations: dict[tuple[str, str], ConversationRecord] = {}
        self.messages: dict[str, StoredMessage] = {}

    async def initialize(self) -> None:
        return None

    async def get_or_create(self, message: IncomingMessage) -> ConversationRecord:
        key = (message.channel, message.external_chat_id)
        if key not in self.conversations:
            self.conversations[key] = ConversationRecord(
                channel=message.channel,
                external_chat_id=message.external_chat_id,
                external_user_id=message.external_user_id,
            )
        return self.conversations[key]

    async def get_by_id(self, conversation_id: UUID) -> ConversationRecord | None:
        return next(
            (
                conversation
                for conversation in self.conversations.values()
                if conversation.id == conversation_id
            ),
            None,
        )

    async def update_status(
        self,
        conversation_id: UUID,
        *,
        status: str | None = None,
        issue_status: str | None = None,
        reason: str | None = None,
    ) -> ConversationRecord:
        conversation = await self.get_by_id(conversation_id)
        if not conversation:
            raise KeyError(f"Conversation not found: {conversation_id}")

        updated = conversation.model_copy(
            update={
                "status": status or conversation.status,
                "issue_status": issue_status or conversation.issue_status,
            }
        )
        key = (updated.channel, updated.external_chat_id)
        self.conversations[key] = updated
        return updated

    async def save_message(self, message: StoredMessage) -> bool:
        if message.event_id in self.messages:
            return False
        self.messages[message.event_id] = message
        return True

    async def list_messages_since(
        self,
        conversation_id: UUID,
        since: datetime,
        limit: int,
    ) -> list[StoredMessage]:
        messages = sorted(
            (
                message
                for message in self.messages.values()
                if message.conversation_id == conversation_id and message.created_at >= since
            ),
            key=lambda message: message.created_at,
        )
        return messages[-limit:]


class MemoryRetrievalStore:
    def __init__(self) -> None:
        self.documents: dict[str, list[Document]] = {}

    async def initialize(self) -> None:
        return None

    async def upsert(self, documents: list[Document], namespace: str) -> None:
        self.documents.setdefault(namespace, []).extend(documents)

    async def search(self, query: str, namespace: str, limit: int = 4) -> list[Document]:
        query_tokens = tokenize(query)

        def score(document: Document) -> float:
            tokens = tokenize(document.page_content)
            return len(query_tokens & tokens) / max(len(query_tokens), 1)

        ranked = sorted(self.documents.get(namespace, []), key=score, reverse=True)
        return [document for document in ranked if score(document) > 0][:limit]


class ExtractiveAnswerGenerator:
    async def generate(
        self,
        query: str,
        documents: list[Document],
        conversation_history: list[StoredMessage] | None = None,
    ) -> tuple[str, float]:
        if not documents:
            return "I could not find enough information to answer that safely.", 0.0
        source = documents[0]
        answer = source.page_content
        overlap = len(tokenize(query) & tokenize(answer)) / max(len(tokenize(query)), 1)
        confidence = min(0.95, 0.55 + overlap)
        return answer, confidence
