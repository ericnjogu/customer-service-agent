from typing import Protocol

from langchain_core.documents import Document

from app.models import ConversationRecord, IncomingMessage, StoredMessage


class ConversationRepository(Protocol):
    async def initialize(self) -> None: ...

    async def get_or_create(self, message: IncomingMessage) -> ConversationRecord: ...

    async def save_message(self, message: StoredMessage) -> bool: ...


class RetrievalStore(Protocol):
    async def initialize(self) -> None: ...

    async def upsert(self, documents: list[Document], namespace: str) -> None: ...

    async def search(self, query: str, namespace: str, limit: int = 4) -> list[Document]: ...


class AnswerGenerator(Protocol):
    async def generate(self, query: str, documents: list[Document]) -> tuple[str, float]: ...
