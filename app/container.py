import logging
from dataclasses import dataclass

from app.adapters.llm import create_openai_answer_generator
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

logger = logging.getLogger(__name__)


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
    logger.info(
        "Creating app container with retrieval_provider=%s seed_knowledge=%s knowledge_path=%s",
        settings.retrieval_provider,
        settings.seed_knowledge,
        settings.knowledge_path or "<unset>",
    )
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
        documents = load_knowledge_documents(settings.knowledge_path)
        logger.info(
            "Loaded %d seed knowledge document(s) for namespace=%s sources=%s",
            len(documents),
            SEED_KNOWLEDGE_NAMESPACE,
            [str(document.metadata.get("source", "unknown")) for document in documents],
        )
        await retrieval.upsert(documents, SEED_KNOWLEDGE_NAMESPACE)
    else:
        logger.info("Seed knowledge loading is disabled")

    logger.info(f"answer provider '{settings.answer_provider}'")
    if settings.answer_provider == "extractive":
        logger.info("using no-op answer generator")
        generator = ExtractiveAnswerGenerator()
    elif settings.answer_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when SUPPORT_ANSWER_PROVIDER=openai"
            )
        logger.info("using openai answer generator")
        generator = create_openai_answer_generator(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            temperature=settings.llm_temperature,
        )
    else:
        raise ValueError(f"Unsupported answer provider: {settings.answer_provider}")

    graph = build_support_graph(
        conversations,
        retrieval,
        generator,
        settings.confidence_threshold,
        settings.conversation_history_max_messages,
    )
    return Container(conversations, retrieval, graph, database)
