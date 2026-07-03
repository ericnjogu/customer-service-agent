from dataclasses import dataclass

from langchain_core.documents import Document

from app.adapters.memory import (
    ExtractiveAnswerGenerator,
    MemoryConversationRepository,
    MemoryRetrievalStore,
)
from app.adapters.postgres import (
    PgVectorRetrievalStore,
    PostgresConversationRepository,
    PostgresDatabase,
)
from app.config import Settings
from app.graph import build_support_graph

SEED_DOCUMENTS = [
    Document(
        page_content=(
            "To reset your password, open Settings, select Security, then choose Reset password. "
            "A reset link will be sent to your verified email address."
        ),
        metadata={"source": "kb/password-reset"},
    ),
    Document(
        page_content=(
            "Refund requests can be submitted within 30 days of purchase. Include the order "
            "number and the reason for the request."
        ),
        metadata={"source": "kb/refunds"},
    ),
]


@dataclass
class Container:
    conversations: object
    retrieval: object
    graph: object
    database: PostgresDatabase | None = None

    async def close(self) -> None:
        if self.database:
            await self.database.close()


async def create_container(settings: Settings) -> Container:
    database = None
    if settings.retrieval_provider == "pgvector":
        if not settings.database_url:
            raise ValueError("SUPPORT_DATABASE_URL is required when using pgvector")
        database = PostgresDatabase(settings.database_url)
        await database.initialize()
        conversations = PostgresConversationRepository(database)
        retrieval = PgVectorRetrievalStore(database)
    elif settings.retrieval_provider == "memory":
        conversations = MemoryConversationRepository()
        retrieval = MemoryRetrievalStore()
    else:
        raise ValueError(f"Unsupported retrieval provider: {settings.retrieval_provider}")

    await conversations.initialize()
    await retrieval.initialize()
    if settings.seed_knowledge:
        await retrieval.upsert(SEED_DOCUMENTS, "knowledge")
    generator = ExtractiveAnswerGenerator()
    graph = build_support_graph(conversations, retrieval, generator, settings.confidence_threshold)
    return Container(conversations, retrieval, graph, database)
