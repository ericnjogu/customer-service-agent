import logging
from dataclasses import dataclass

from app.adapters.embeddings import LocalHashEmbeddingProvider, OpenAIEmbeddingProvider
from app.adapters.ingestion_queue import MemoryKnowledgeIngestionQueue, RedisKnowledgeIngestionQueue
from app.adapters.llm import (
    create_openai_answer_generator,
    create_openai_human_request_detector,
    create_openai_question_planner,
)
from app.adapters.memory import (
    ExtractiveAnswerGenerator,
    MemoryConversationRepository,
    MemoryKnowledgeIngestionJobRepository,
    MemoryRetrievalStore,
    MemoryTenantConfigRepository,
    MemoryTenantRepository,
    RuleBasedHumanRequestDetector,
    RuleBasedQuestionPlanner,
)
from app.adapters.object_store import MemoryKnowledgeObjectStore, S3KnowledgeObjectStore
from app.adapters.postgres import (
    PgVectorRetrievalStore,
    PostgresConversationRepository,
    PostgresDatabase,
    PostgresKnowledgeIngestionJobRepository,
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
from app.graph import build_support_graph
from app.ingestion import (
    KnowledgeIngestionService,
    KnowledgeIngestionWorker,
    OpenAIVisionOcrClient,
    PdfOcrFallbackExtractor,
)
from app.knowledge import load_knowledge_documents, tenant_knowledge_namespace

logger = logging.getLogger(__name__)


@dataclass
class Container:
    conversations: object
    tenants: object
    tenant_configs: object
    knowledge_ingestion_jobs: object
    knowledge_object_store: object
    knowledge_ingestion_queue: object
    retrieval: object
    knowledge_ingestion: KnowledgeIngestionService
    knowledge_ingestion_worker: KnowledgeIngestionWorker
    graph: object
    telegram_credentials: TelegramCredentialResolver
    telegram_sender: TelegramSender | None = None
    whatsapp_sender: WhatsAppSender | None = None
    database: PostgresDatabase | None = None

    async def close(self) -> None:
        close_tenant_configs = getattr(self.tenant_configs, "close", None)
        if close_tenant_configs:
            await close_tenant_configs()
        close_ingestion_queue = getattr(self.knowledge_ingestion_queue, "close", None)
        if close_ingestion_queue:
            await close_ingestion_queue()
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
        tenants = PostgresTenantRepository(database)
        knowledge_ingestion_jobs = PostgresKnowledgeIngestionJobRepository(database)
        tenant_configs = PostgresTenantConfigRepository(
            database,
            default_vector_collection=settings.vector_collection,
        )
        retrieval = PgVectorRetrievalStore(database, embeddings)
    elif settings.retrieval_provider == "memory":
        conversations = MemoryConversationRepository()
        tenants = MemoryTenantRepository()
        knowledge_ingestion_jobs = MemoryKnowledgeIngestionJobRepository()
        tenant_configs = MemoryTenantConfigRepository(
            default_vector_collection=settings.vector_collection,
        )
        retrieval = MemoryRetrievalStore()
    else:
        raise ValueError(f"Unsupported retrieval provider: {settings.retrieval_provider}")

    if settings.tenant_config_cache_provider == "redis":
        if not settings.redis_url:
            raise ValueError(
                "SUPPORT_REDIS_URL is required when "
                "SUPPORT_TENANT_CONFIG_CACHE_PROVIDER=redis"
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

    if settings.knowledge_object_store_provider == "memory":
        knowledge_object_store = MemoryKnowledgeObjectStore(
            bucket=settings.knowledge_object_store_bucket
        )
    elif settings.knowledge_object_store_provider == "s3":
        if (
            not settings.s3_endpoint_url
            or not settings.s3_access_key_id
            or not settings.s3_secret_access_key
        ):
            raise ValueError(
                "SUPPORT_S3_ENDPOINT_URL, SUPPORT_S3_ACCESS_KEY_ID, and "
                "SUPPORT_S3_SECRET_ACCESS_KEY are required when "
                "SUPPORT_KNOWLEDGE_OBJECT_STORE_PROVIDER=s3"
            )
        knowledge_object_store = S3KnowledgeObjectStore(
            endpoint_url=settings.s3_endpoint_url,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            bucket=settings.knowledge_object_store_bucket,
            region_name=settings.s3_region_name,
            secure=settings.s3_secure,
        )
    else:
        raise ValueError(
            "Unsupported knowledge object store provider: "
            f"{settings.knowledge_object_store_provider}"
        )

    if settings.knowledge_ingestion_queue_provider == "redis":
        if not settings.redis_url:
            raise ValueError(
                "SUPPORT_REDIS_URL is required when "
                "SUPPORT_KNOWLEDGE_INGESTION_QUEUE_PROVIDER=redis"
            )
        knowledge_ingestion_queue = RedisKnowledgeIngestionQueue(
            create_redis_client(settings.redis_url),
            queue_name=settings.knowledge_ingestion_queue_name,
        )
    elif settings.knowledge_ingestion_queue_provider == "memory":
        knowledge_ingestion_queue = MemoryKnowledgeIngestionQueue()
    else:
        raise ValueError(
            "Unsupported knowledge ingestion queue provider: "
            f"{settings.knowledge_ingestion_queue_provider}"
        )

    await conversations.initialize()
    await tenants.initialize()
    await tenant_configs.initialize()
    await knowledge_ingestion_jobs.initialize()
    await knowledge_object_store.initialize()
    await retrieval.initialize()
    if settings.seed_knowledge:
        knowledge_namespace = tenant_knowledge_namespace(settings.default_tenant_id)
        documents = load_knowledge_documents(
            settings.knowledge_path,
            chunk_size=settings.knowledge_chunk_size,
            chunk_overlap=settings.knowledge_chunk_overlap,
        )
        logger.info(
            "Loaded %d seed knowledge chunk(s) for namespace=%s sources=%s",
            len(documents),
            knowledge_namespace,
            [str(document.metadata.get("source", "unknown")) for document in documents],
        )
        await retrieval.upsert(documents, knowledge_namespace)
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
    pdf_extractor = None
    if settings.knowledge_pdf_ocr_provider == "openai":
        if not settings.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when SUPPORT_KNOWLEDGE_PDF_OCR_PROVIDER=openai"
            )
        pdf_extractor = PdfOcrFallbackExtractor(
            ocr_client=OpenAIVisionOcrClient(
                api_key=settings.openai_api_key,
                model=settings.knowledge_pdf_ocr_model or settings.llm_model,
            ),
            render_dpi=settings.knowledge_pdf_ocr_dpi,
        )
    elif settings.knowledge_pdf_ocr_provider != "none":
        raise ValueError(
            "Unsupported PDF OCR provider: "
            f"{settings.knowledge_pdf_ocr_provider}"
        )

    knowledge_ingestion = KnowledgeIngestionService(
        retrieval,
        tenant_configs,
        chunk_size=settings.knowledge_chunk_size,
        chunk_overlap=settings.knowledge_chunk_overlap,
        pdf_extractor=pdf_extractor,
    )
    knowledge_ingestion_worker = KnowledgeIngestionWorker(
        jobs=knowledge_ingestion_jobs,
        object_store=knowledge_object_store,
        ingestion_service=knowledge_ingestion,
    )
    return Container(
        conversations=conversations,
        tenants=tenants,
        tenant_configs=tenant_configs,
        knowledge_ingestion_jobs=knowledge_ingestion_jobs,
        knowledge_object_store=knowledge_object_store,
        knowledge_ingestion_queue=knowledge_ingestion_queue,
        retrieval=retrieval,
        knowledge_ingestion=knowledge_ingestion,
        knowledge_ingestion_worker=knowledge_ingestion_worker,
        graph=graph,
        telegram_credentials=telegram_credentials,
        telegram_sender=telegram_sender,
        whatsapp_sender=whatsapp_sender,
        database=database,
    )
