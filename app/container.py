import logging
from dataclasses import dataclass

from app.adapters.embeddings import LocalHashEmbeddingProvider, OpenAIEmbeddingProvider
from app.adapters.llm import (
    create_openai_answer_generator,
    create_openai_human_request_detector,
    create_openai_question_planner,
)
from app.adapters.memory import (
    ExtractiveAnswerGenerator,
    MemoryConversationRepository,
    MemoryRetrievalStore,
    RuleBasedHumanRequestDetector,
    RuleBasedQuestionPlanner,
)
from app.adapters.postgres import (
    PgVectorRetrievalStore,
    PostgresConversationRepository,
    PostgresDatabase,
)
from app.adapters.telegram import TelegramBotClient, TelegramSender
from app.adapters.whatsapp import WhatsAppCloudClient, WhatsAppSender
from app.config import Settings
from app.graph import build_support_graph
from app.knowledge import SEED_KNOWLEDGE_NAMESPACE, load_knowledge_documents

logger = logging.getLogger(__name__)


@dataclass
class Container:
    conversations: object
    retrieval: object
    graph: object
    telegram_sender: TelegramSender | None = None
    whatsapp_sender: WhatsAppSender | None = None
    database: PostgresDatabase | None = None

    async def close(self) -> None:
        if self.database:
            await self.database.close()


async def create_container(settings: Settings) -> Container:
    database = None
    if settings.knowledge_chunk_overlap >= settings.knowledge_chunk_size:
        raise ValueError("SUPPORT_KNOWLEDGE_CHUNK_OVERLAP must be smaller than chunk size")
    logger.info(
        "Creating app container with retrieval_provider=%s embedding_provider=%s "
        "embedding_dimensions=%s seed_knowledge=%s knowledge_path=%s",
        settings.retrieval_provider,
        settings.embedding_provider,
        settings.embedding_dimensions,
        settings.seed_knowledge,
        settings.knowledge_path or "<unset>",
    )

    if settings.embedding_provider == "local":
        embeddings = LocalHashEmbeddingProvider(settings.embedding_dimensions)
    elif settings.embedding_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when SUPPORT_EMBEDDING_PROVIDER=openai")
        embeddings = OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    else:
        raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")

    if settings.retrieval_provider == "pgvector":
        if not settings.database_url:
            raise ValueError("SUPPORT_DATABASE_URL is required when using pgvector")
        database = PostgresDatabase(
            settings.database_url,
            embedding_dimensions=embeddings.dimensions,
        )
        await database.initialize()
        conversations = PostgresConversationRepository(database)
        retrieval = PgVectorRetrievalStore(database, embeddings)
    elif settings.retrieval_provider == "memory":
        conversations = MemoryConversationRepository()
        retrieval = MemoryRetrievalStore()
    else:
        raise ValueError(f"Unsupported retrieval provider: {settings.retrieval_provider}")

    await conversations.initialize()
    await retrieval.initialize()
    if settings.seed_knowledge:
        documents = load_knowledge_documents(
            settings.knowledge_path,
            chunk_size=settings.knowledge_chunk_size,
            chunk_overlap=settings.knowledge_chunk_overlap,
        )
        logger.info(
            "Loaded %d seed knowledge chunk(s) for namespace=%s sources=%s",
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

    logger.info("human request detector provider '%s'", settings.human_request_detector_provider)
    logger.info("question planner provider '%s'", settings.question_planner_provider)
    if settings.question_planner_provider == "rules":
        question_planner = RuleBasedQuestionPlanner()
    elif settings.question_planner_provider == "llm":
        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when SUPPORT_QUESTION_PLANNER_PROVIDER=llm"
            )
        question_planner = create_openai_question_planner(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            temperature=0.0,
        )
    else:
        raise ValueError(
            "Unsupported question planner provider: "
            f"{settings.question_planner_provider}"
        )

    if settings.human_request_detector_provider == "rules":
        human_request_detector = RuleBasedHumanRequestDetector()
    elif settings.human_request_detector_provider == "llm":
        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when SUPPORT_HUMAN_REQUEST_DETECTOR_PROVIDER=llm"
            )
        human_request_detector = create_openai_human_request_detector(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            temperature=0.0,
        )
    else:
        raise ValueError(
            "Unsupported human request detector provider: "
            f"{settings.human_request_detector_provider}"
        )

    graph = build_support_graph(
        conversations,
        retrieval,
        generator,
        question_planner,
        human_request_detector,
        settings.confidence_threshold,
        settings.conversation_history_max_messages,
        settings.greeting_lapse_minutes,
    )
    telegram_sender = (
        TelegramBotClient(settings.telegram_bot_token)
        if settings.telegram_bot_token
        else None
    )
    whatsapp_sender = (
        WhatsAppCloudClient(
            settings.whatsapp_access_token,
            settings.whatsapp_phone_number_id,
            settings.whatsapp_graph_api_version,
        )
        if settings.whatsapp_access_token and settings.whatsapp_phone_number_id
        else None
    )
    return Container(
        conversations=conversations,
        retrieval=retrieval,
        graph=graph,
        telegram_sender=telegram_sender,
        whatsapp_sender=whatsapp_sender,
        database=database,
    )
