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
    MemoryTenantConfigRepository,
    MemoryTenantRepository,
    RuleBasedHumanRequestDetector,
    RuleBasedQuestionPlanner,
)
from app.adapters.postgres import (
    PgVectorRetrievalStore,
    PostgresConversationRepository,
    PostgresDatabase,
    PostgresTenantConfigRepository,
    PostgresTenantRepository,
)
from app.adapters.telegram import (
    KubernetesSecretTelegramCredentialResolver,
    StaticTelegramCredentialResolver,
    TelegramCredentialResolver,
    TelegramSender,
    TenantAwareTelegramSender,
)
from app.adapters.tenant_cache import (
    MemoryCachedTenantConfigRepository,
    RedisTenantConfigRepository,
    create_redis_client,
)
from app.adapters.whatsapp import WhatsAppCloudClient, WhatsAppSender
from app.config import Settings
from app.graph import build_service_graph

logger = logging.getLogger(__name__)


@dataclass
class Container:
    conversations: object
    tenants: object
    tenant_configs: object
    retrieval: object
    graph: object
    telegram_credentials: TelegramCredentialResolver
    telegram_sender: TelegramSender | None = None
    whatsapp_sender: WhatsAppSender | None = None
    database: PostgresDatabase | None = None

    async def close(self) -> None:
        close_tenant_configs = getattr(self.tenant_configs, "close", None)
        if close_tenant_configs:
            await close_tenant_configs()
        if self.database:
            await self.database.close()


async def create_container(settings: Settings) -> Container:
    database = None
    logger.info(
        "Creating app container with retrieval_provider=%s embedding_provider=%s "
        "embedding_dimensions=%s",
        settings.retrieval_provider,
        settings.embedding_provider,
        settings.embedding_dimensions,
    )

    if settings.embedding_provider == "local":
        embeddings = LocalHashEmbeddingProvider(settings.embedding_dimensions)
    elif settings.embedding_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when AGENT_EMBEDDING_PROVIDER=openai")
        embeddings = OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    else:
        raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")

    if settings.retrieval_provider == "pgvector":
        if not settings.database_url:
            raise ValueError("AGENT_DATABASE_URL is required when using pgvector")
        database = PostgresDatabase(
            settings.database_url,
            embedding_dimensions=embeddings.dimensions,
        )
        await database.initialize()
        conversations = PostgresConversationRepository(database)
        tenants = PostgresTenantRepository(database)
        tenant_configs = PostgresTenantConfigRepository(
            database,
            default_vector_collection=settings.vector_collection,
        )
        retrieval = PgVectorRetrievalStore(database, embeddings)
    elif settings.retrieval_provider == "memory":
        conversations = MemoryConversationRepository()
        tenants = MemoryTenantRepository()
        tenant_configs = MemoryTenantConfigRepository(
            default_vector_collection=settings.vector_collection,
        )
        retrieval = MemoryRetrievalStore()
    else:
        raise ValueError(f"Unsupported retrieval provider: {settings.retrieval_provider}")

    if settings.tenant_config_cache_provider == "redis":
        if not settings.redis_url:
            raise ValueError(
                "AGENT_REDIS_URL is required when "
                "AGENT_TENANT_CONFIG_CACHE_PROVIDER=redis"
            )
        tenant_configs = RedisTenantConfigRepository(
            tenant_configs,
            create_redis_client(settings.redis_url),
            ttl_seconds=settings.tenant_config_cache_ttl_seconds,
        )
    elif settings.tenant_config_cache_provider == "memory":
        tenant_configs = MemoryCachedTenantConfigRepository(tenant_configs)
    else:
        raise ValueError(
            "Unsupported tenant config cache provider: "
            f"{settings.tenant_config_cache_provider}"
        )

    await conversations.initialize()
    await tenants.initialize()
    await tenant_configs.initialize()
    await retrieval.initialize()

    logger.info(f"answer provider '{settings.answer_provider}'")
    if settings.answer_provider == "extractive":
        logger.info("using no-op answer generator")
        generator = ExtractiveAnswerGenerator()
    elif settings.answer_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when AGENT_ANSWER_PROVIDER=openai"
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
                "OPENAI_API_KEY is required when AGENT_QUESTION_PLANNER_PROVIDER=llm"
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
                "OPENAI_API_KEY is required when AGENT_HUMAN_REQUEST_DETECTOR_PROVIDER=llm"
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

    graph = build_service_graph(
        conversations,
        tenant_configs,
        retrieval,
        generator,
        question_planner,
        human_request_detector,
        settings.confidence_threshold,
        settings.conversation_history_max_messages,
        settings.greeting_lapse_minutes,
    )
    static_telegram_credentials = StaticTelegramCredentialResolver(
        bot_token=settings.telegram_bot_token,
        webhook_secret_token=settings.telegram_webhook_secret_token,
    )
    if settings.telegram_credential_provider == "kubernetes":
        telegram_credentials = KubernetesSecretTelegramCredentialResolver(
            tenant_configs=tenant_configs,
            fallback=static_telegram_credentials,
            namespace=settings.telegram_secret_namespace,
            bot_token_key=settings.telegram_bot_token_secret_key,
            webhook_secret_token_key=settings.telegram_webhook_secret_token_secret_key,
        )
    elif settings.telegram_credential_provider == "static":
        telegram_credentials = static_telegram_credentials
    else:
        raise ValueError(
            "Unsupported Telegram credential provider: "
            f"{settings.telegram_credential_provider}"
        )
    telegram_sender = TenantAwareTelegramSender(telegram_credentials)
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
        tenants=tenants,
        tenant_configs=tenant_configs,
        retrieval=retrieval,
        graph=graph,
        telegram_credentials=telegram_credentials,
        telegram_sender=telegram_sender,
        whatsapp_sender=whatsapp_sender,
        database=database,
    )
