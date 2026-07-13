from datetime import datetime
from typing import Protocol
from uuid import UUID

from langchain_core.documents import Document

from app.models import (
    ConversationRecord,
    IncomingMessage,
    StoredMessage,
)


class ConversationRepository(Protocol):
    async def initialize(self) -> None: ...

    async def get_or_create(self, message: IncomingMessage) -> ConversationRecord: ...

    async def get_by_id(self, conversation_id: UUID) -> ConversationRecord | None: ...

    async def update_status(
        self,
        conversation_id: UUID,
        *,
        status: str | None = None,
        issue_status: str | None = None,
        reason: str | None = None,
    ) -> ConversationRecord: ...

    async def save_message(self, message: StoredMessage) -> bool: ...

    async def list_messages_since(
        self,
        conversation_id: UUID,
        since: datetime,
        limit: int,
    ) -> list[StoredMessage]: ...


class RetrievalStore(Protocol):
    async def initialize(self) -> None: ...

    async def upsert(self, documents: list[Document], namespace: str) -> None: ...

    async def search(self, query: str, namespace: str, limit: int = 4) -> list[Document]: ...


class AnswerGenerator(Protocol):
    async def generate(
        self,
        query: str,
        documents: list[Document],
        conversation_history: list[StoredMessage] | None = None,
    ) -> tuple[str, float]: ...
