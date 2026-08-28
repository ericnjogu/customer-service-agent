import logging
from dataclasses import dataclass

from app.adapters.embeddings import LocalHashEmbeddingProvider, OpenAIEmbeddingProvider
from app.adapters.llm import (
    MissingOpenAIWebsiteAnalyzer,
    NoopWebsiteResearcher,
    TavilyWebsiteResearcher,
    create_openai_answer_generator,
    create_openai_question_planner,
    create_openai_website_analyzer,
)
from app.adapters.memory import (
    ExtractiveAnswerGenerator,
    MemoryConversationRepository,
    MemoryOnboardingRepository,
    MemoryRetrievalStore,
    MemoryTenantConfigRepository,
    MemoryTenantRepository,
    RuleBasedQuestionPlanner,
)
from app.adapters.postgres import (
    PgVectorRetrievalStore,
    PostgresConversationRepository,
    PostgresDatabase,
    PostgresOnboardingRepository,
    PostgresTenantConfigRepository,
    PostgresTenantRepository,
)
from app.adapters.telegram import (
    KubernetesSecretTelegramCredentialResolver,
    KubernetesSecretTelegramSecretWriter,
    TelegramBotWebhookRegistrar,
    TelegramCredentialResolver,
    TelegramSecretWriter,
    TelegramSender,
    TelegramWebhookRegistrar,
    TenantAwareTelegramSender,
)
from app.adapters.tenant_cache import (
    MemoryCachedTenantConfigRepository,
    RedisTenantConfigRepository,
    create_redis_client,
)
from app.adapters.whatsapp import (
    KubernetesSecretWhatsAppCredentialResolver,
    TenantAwareWhatsAppSender,
    WhatsAppCredentialResolver,
    WhatsAppSender,
)
from app.config import Settings
from app.graph import build_service_graph
from app.notifications import LoggingEmailSender, ResendEmailSender
from app.onboarding_jobs import OnboardingJobService
from app.onboarding_sessions import OnboardingSessionService
from app.provider_projects import (
    MetadataOnlyProviderProjectProvisioner,
    OpenAILangSmithProviderProjectProvisioner,
)

logger = logging.getLogger(__name__)


def create_platform_website_researcher(settings: Settings) -> object:
    provider = settings.platform_web_search_provider.strip().lower()
    if provider in {"", "none", "disabled"}:
        logger.info("Using no-op platform website researcher")
        return NoopWebsiteResearcher()
    if provider == "tavily":
        if not settings.platform_web_search_api_key:
            raise ValueError(
                "AGENT_PLATFORM_WEB_SEARCH_API_KEY is required when "
                "AGENT_PLATFORM_WEB_SEARCH_PROVIDER=tavily"
            )
        logger.info(
            "Using Tavily platform website researcher project_id=%s max_results=%s "
            "timeout_seconds=%s",
            settings.platform_web_search_project_id,
            settings.platform_web_search_max_results,
            settings.platform_web_search_timeout_seconds,
        )
        return TavilyWebsiteResearcher(
            api_key=settings.platform_web_search_api_key,
            project_id=settings.platform_web_search_project_id,
            max_results=settings.platform_web_search_max_results,
            timeout_seconds=settings.platform_web_search_timeout_seconds,
        )
    raise ValueError(
        f"Unsupported platform web search provider: {settings.platform_web_search_provider}"
    )


def create_provider_project_provisioner(settings: Settings) -> object:
    provider = settings.provider_project_provisioner.strip().lower()
    if provider in {"", "metadata", "none"}:
        logger.info("Using metadata-only provider project provisioner")
        return MetadataOnlyProviderProjectProvisioner()
    if provider == "api":
        if not settings.openai_admin_key:
            raise ValueError(
                "OPENAI_ADMIN_KEY is required when "
                "AGENT_PROVIDER_PROJECT_PROVISIONER=api"
            )
        if not settings.langsmith_api_key:
            raise ValueError(
                "LANGSMITH_API_KEY is required when "
                "AGENT_PROVIDER_PROJECT_PROVISIONER=api"
            )
        logger.info(
            "Using OpenAI/LangSmith provider project provisioner "
            "langsmith_endpoint=%s langsmith_workspace_configured=%s",
            settings.langsmith_endpoint,
            bool(settings.langsmith_workspace_id),
        )
        return OpenAILangSmithProviderProjectProvisioner(
            openai_admin_key=settings.openai_admin_key,
            langsmith_api_key=settings.langsmith_api_key,
            langsmith_endpoint=settings.langsmith_endpoint,
            langsmith_workspace_id=settings.langsmith_workspace_id,
        )
    raise ValueError(
        f"Unsupported provider project provisioner: {settings.provider_project_provisioner}"
    )


@dataclass
class Container:
    conversations: object
    tenants: object
    tenant_configs: object
    onboarding: object
    onboarding_jobs: OnboardingJobService
    onboarding_sessions: OnboardingSessionService
    email_sender: object
    website_analyzer: object
    provider_project_provisioner: object
    retrieval: object
    graph: object
    telegram_credentials: TelegramCredentialResolver
    telegram_secret_writer: TelegramSecretWriter
    telegram_webhook_registrar: TelegramWebhookRegistrar
    whatsapp_credentials: WhatsAppCredentialResolver
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
        onboarding = PostgresOnboardingRepository(database)
        retrieval = PgVectorRetrievalStore(database, embeddings)
    elif settings.retrieval_provider == "memory":
        conversations = MemoryConversationRepository()
        tenants = MemoryTenantRepository()
        tenant_configs = MemoryTenantConfigRepository(
            default_vector_collection=settings.vector_collection,
        )
        onboarding = MemoryOnboardingRepository()
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
    await onboarding.initialize()
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

    graph = build_service_graph(
        conversations,
        tenant_configs,
        retrieval,
        generator,
        question_planner,
        settings.confidence_threshold,
        settings.conversation_history_max_messages,
        settings.greeting_lapse_minutes,
    )
    if settings.email_provider == "log":
        logger.info("Using logging email provider")
        email_sender = LoggingEmailSender()
    elif settings.email_provider == "resend":
        if not settings.resend_api_key:
            raise ValueError("RESEND_API_KEY is required when AGENT_EMAIL_PROVIDER=resend")
        if not settings.email_from:
            raise ValueError(
                "AGENT_EMAIL_FROM is required when AGENT_EMAIL_PROVIDER=resend "
                "(Helm value: email.from; local deploy variable: AGENT_EMAIL_FROM)"
            )
        logger.info(
            "Using Resend email provider from_email=%s review_email_configured=%s",
            settings.email_from,
            bool(settings.onboarding_review_email),
        )
        email_sender = ResendEmailSender(
            api_key=settings.resend_api_key,
            from_email=settings.email_from,
        )
    else:
        raise ValueError(f"Unsupported email provider: {settings.email_provider}")

    provider_project_provisioner = create_provider_project_provisioner(settings)
    telegram_secret_writer = KubernetesSecretTelegramSecretWriter(
        namespace=settings.telegram_secret_namespace,
        bot_token_key=settings.telegram_bot_token_secret_key,
        webhook_secret_token_key=settings.telegram_webhook_secret_token_secret_key,
    )
    telegram_webhook_registrar = TelegramBotWebhookRegistrar(
        public_base_url=settings.telegram_webhook_public_base_url,
    )
    onboarding_jobs = OnboardingJobService(
        onboarding=onboarding,
        tenants=tenants,
        tenant_configs=tenant_configs,
        retrieval=retrieval,
        provider_project_provisioner=provider_project_provisioner,
        telegram_secret_writer=telegram_secret_writer,
        telegram_webhook_registrar=telegram_webhook_registrar,
        email_sender=email_sender,
        onboarding_review_email=settings.onboarding_review_email,
    )
    logger.info(
        "onboarding website analysis provider '%s'",
        settings.onboarding_website_analysis_provider,
    )
    if settings.onboarding_website_analysis_provider == "openai":
        if not settings.openai_api_key:
            logger.warning(
                "OPENAI_API_KEY is not configured; onboarding website analysis "
                "will fail when requested"
            )
            website_analyzer = MissingOpenAIWebsiteAnalyzer()
        else:
            website_researcher = create_platform_website_researcher(settings)
            website_analyzer = create_openai_website_analyzer(
                api_key=settings.openai_api_key,
                model=settings.llm_model,
                website_researcher=website_researcher,
                fetch_timeout_seconds=settings.onboarding_website_fetch_timeout_seconds,
            )
    else:
        raise ValueError(
            "Unsupported onboarding website analysis provider: "
            f"{settings.onboarding_website_analysis_provider}"
        )
    onboarding_sessions = OnboardingSessionService(
        onboarding=onboarding,
        onboarding_jobs=onboarding_jobs,
        tenants=tenants,
        email_sender=email_sender,
        website_analyzer=website_analyzer,
        settings=settings,
    )
    if settings.telegram_credential_provider == "kubernetes":
        telegram_credentials = KubernetesSecretTelegramCredentialResolver(
            tenant_configs=tenant_configs,
            namespace=settings.telegram_secret_namespace,
            bot_token_key=settings.telegram_bot_token_secret_key,
            webhook_secret_token_key=settings.telegram_webhook_secret_token_secret_key,
        )
    else:
        raise ValueError(
            "Unsupported Telegram credential provider: "
            f"{settings.telegram_credential_provider}"
        )
    telegram_sender = TenantAwareTelegramSender(telegram_credentials)
    whatsapp_credentials = KubernetesSecretWhatsAppCredentialResolver(
        tenant_configs=tenant_configs,
        namespace=settings.whatsapp_secret_namespace,
        access_token_key=settings.whatsapp_access_token_secret_key,
        phone_number_id_key=settings.whatsapp_phone_number_id_secret_key,
        verify_token_key=settings.whatsapp_verify_token_secret_key,
        graph_api_version_key=settings.whatsapp_graph_api_version_secret_key,
        default_graph_api_version=settings.whatsapp_graph_api_version,
    )
    whatsapp_sender = TenantAwareWhatsAppSender(whatsapp_credentials)
    return Container(
        conversations=conversations,
        tenants=tenants,
        tenant_configs=tenant_configs,
        onboarding=onboarding,
        onboarding_jobs=onboarding_jobs,
        onboarding_sessions=onboarding_sessions,
        email_sender=email_sender,
        website_analyzer=website_analyzer,
        provider_project_provisioner=provider_project_provisioner,
        retrieval=retrieval,
        graph=graph,
        telegram_credentials=telegram_credentials,
        telegram_secret_writer=telegram_secret_writer,
        telegram_webhook_registrar=telegram_webhook_registrar,
        whatsapp_credentials=whatsapp_credentials,
        telegram_sender=telegram_sender,
        whatsapp_sender=whatsapp_sender,
        database=database,
    )
