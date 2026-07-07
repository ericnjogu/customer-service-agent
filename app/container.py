from dataclasses import dataclass

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
from app.knowledge import SEED_KNOWLEDGE_NAMESPACE, load_knowledge_documents


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
        await retrieval.upsert(
            load_knowledge_documents(settings.knowledge_path),
            SEED_KNOWLEDGE_NAMESPACE,
        )
    generator = ExtractiveAnswerGenerator()
    graph = build_support_graph(conversations, retrieval, generator, settings.confidence_threshold)
    return Container(conversations, retrieval, graph, database)
