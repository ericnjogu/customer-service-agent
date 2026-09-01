from functools import lru_cache

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env")

    app_name: str = "customer-service-agent"
    default_tenant_id: str = Field(default="default", min_length=1)
    database_url: str | None = None
    retrieval_provider: str = "memory"
    answer_provider: str = "extractive"
    embedding_provider: str = "local"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=64, gt=0)
    kb_chunk_size: int = Field(default=1_000, gt=0)
    kb_chunk_overlap: int = Field(default=180, ge=0)
    question_planner_provider: str = "rules"
    llm_model: str = "gpt-4.1-mini"
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    llm_temperature: float = 0.0
    confidence_threshold: float = 0.60
    conversation_history_max_messages: int = Field(default=50, gt=0)
    greeting_lapse_minutes: int = Field(default=60, gt=0)
    tenant_config_cache_provider: str = "memory"
    tenant_config_cache_ttl_seconds: int = Field(default=300, gt=0)
    redis_url: str | None = None
    vector_collection: str = Field(default="customer-service", min_length=1)
    telegram_credential_provider: str = "kubernetes"
    telegram_secret_namespace: str | None = None
    telegram_bot_token_secret_key: str = "TELEGRAM_BOT_TOKEN"
    telegram_webhook_secret_token_secret_key: str = "TELEGRAM_WEBHOOK_SECRET_TOKEN"
    telegram_webhook_public_base_url: str | None = None
    whatsapp_secret_namespace: str | None = None
    whatsapp_access_token_secret_key: str = "WHATSAPP_ACCESS_TOKEN"
    whatsapp_phone_number_id_secret_key: str = "WHATSAPP_PHONE_NUMBER_ID"
    whatsapp_verify_token_secret_key: str = "WHATSAPP_VERIFY_TOKEN"
    whatsapp_graph_api_version_secret_key: str = "WHATSAPP_GRAPH_API_VERSION"
    whatsapp_graph_api_version: str = "v20.0"
    web_public_base_url: str = "http://localhost:5173"
    onboarding_action_token_ttl_minutes: int = Field(default=60, gt=0)
    onboarding_email_verification_token_ttl_minutes: int = Field(default=60, gt=0)
    onboarding_require_admin_email_domain_match: bool = True
    onboarding_website_analysis_provider: str = "openai"
    onboarding_website_fetch_timeout_seconds: int = Field(default=10, gt=0)
    platform_web_search_provider: str = "none"
    platform_web_search_project_id: str = "ristoh-css"
    platform_web_search_max_results: int = Field(default=3, gt=0)
    platform_web_search_timeout_seconds: int = Field(default=15, gt=0)
    platform_web_search_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AGENT_PLATFORM_WEB_SEARCH_API_KEY",
            "TAVILY_API_KEY",
        ),
    )
    runtime_web_search_provider: str = "tavily"
    provider_project_provisioner: str = "metadata"
    openai_admin_key: str | None = Field(default=None, validation_alias="OPENAI_ADMIN_KEY")
    langsmith_api_key: str | None = Field(default=None, validation_alias="LANGSMITH_API_KEY")
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com",
        validation_alias="LANGSMITH_ENDPOINT",
    )
    langsmith_workspace_id: str | None = Field(
        default=None,
        validation_alias="LANGSMITH_WORKSPACE_ID",
    )
    email_provider: str = "log"
    email_from: str | None = None
    onboarding_review_email: str | None = None
    resend_api_key: str | None = Field(default=None, validation_alias="RESEND_API_KEY")
    cors_allow_origins: str = "*"
    log_level: str = "INFO"
    log_format: str = "{asctime} - {levelname}:{name}:{message}"

    @model_validator(mode="after")
    def validate_kb_chunk_settings(self) -> "Settings":
        if self.kb_chunk_overlap >= self.kb_chunk_size:
            raise ValueError("AGENT_KB_CHUNK_OVERLAP must be smaller than AGENT_KB_CHUNK_SIZE")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
