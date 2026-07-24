import re
from datetime import datetime
from uuid import UUID

from langchain_core.documents import Document

from app.models import (
    ConversationPromptMetadata,
    ConversationRecord,
    IncomingMessage,
    QuestionPlan,
    StoredMessage,
)

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


def normalize_token(token: str) -> str:
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {normalize_token(token) for token in tokens} - STOP_WORDS


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

    async def update_state(
        self,
        conversation_id: UUID,
        *,
        state: str,
        reason: str | None = None,
    ) -> ConversationRecord:
        conversation = await self.get_by_id(conversation_id)
        if not conversation:
            raise KeyError(f"Conversation not found: {conversation_id}")

        updated = conversation.model_copy(
            update={
                "state": state,
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

    async def minutes_since_previous_customer_message(
        self,
        conversation_id: UUID,
        current_event_id: str,
        current_received_at: datetime,
    ) -> int | None:
        previous_customer_message = max(
            (
                message
                for message in self.messages.values()
                if message.conversation_id == conversation_id
                and message.sender_type == "CUSTOMER"
                and message.event_id != current_event_id
            ),
            key=lambda message: message.created_at,
            default=None,
        )
        if previous_customer_message is None:
            return None

        return max(
            0,
            int(
                (current_received_at - previous_customer_message.created_at).total_seconds()
                // 60
            ),
        )


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
        conversation_metadata: ConversationPromptMetadata | None = None,
    ) -> tuple[str, float]:
        if not documents:
            return "I could not find enough information to answer that safely.", 0.0
        source = documents[0]
        answer = source.page_content
        overlap = len(tokenize(query) & tokenize(answer)) / max(len(tokenize(query)), 1)
        confidence = min(0.95, 0.55 + overlap)
        return answer, confidence

class RuleBasedHumanRequestDetector:
    human_request_phrases = (
        "human agent",
        "human support",
        "real person",
        "talk to someone",
        "speak to someone",
        "support team member",
        "customer support agent",
        "manager",
    )

    async def detect(
        self,
        message: IncomingMessage,
        conversation_history: list[StoredMessage] | None = None,
    ) -> bool:
        text = message.text.lower()
        return any(phrase in text for phrase in self.human_request_phrases)


class RuleBasedQuestionPlanner:
    out_of_scope_phrases = (
        "tell me a riddle",
        "write code",
        "solve this",
        "calculate",
    )
    history_cues = (
        "that",
        "this",
        "those",
        "it",
        "again",
        "previous",
        "earlier",
        "still",
        "same",
    )

    async def plan(
        self,
        message: IncomingMessage,
        conversation_metadata: ConversationPromptMetadata | None = None,
    ) -> QuestionPlan:
        text = message.text.lower().strip()
        if self._is_arithmetic_question(text) or any(
            phrase in text for phrase in self.out_of_scope_phrases
        ):
            return QuestionPlan(
                in_scope=False,
                needs_conversation_history=False,
                explanation=(
                    "I can help with questions about this business, its services, "
                    "orders, bookings, policies, or support."
                ),
            )

        return QuestionPlan(
            in_scope=True,
            needs_conversation_history=any(cue in tokenize(text) for cue in self.history_cues),
            explanation="local heuristic planner",
        )

    def _is_arithmetic_question(self, text: str) -> bool:
        arithmetic_expression = r"[\d\s.,()+\-*/x×÷=%?]+"
        if re.fullmatch(arithmetic_expression, text):
            return bool(re.search(r"\d", text) and re.search(r"[+\-*/x×÷=%]", text))

        math_prefix = r"(what\s+is|what's|calculate|compute|solve)"
        return bool(re.fullmatch(rf"{math_prefix}\s+{arithmetic_expression}", text))
