from functools import lru_cache

from pydantic import Field
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
    whatsapp_secret_namespace: str | None = None
    whatsapp_access_token_secret_key: str = "WHATSAPP_ACCESS_TOKEN"
    whatsapp_phone_number_id_secret_key: str = "WHATSAPP_PHONE_NUMBER_ID"
    whatsapp_verify_token_secret_key: str = "WHATSAPP_VERIFY_TOKEN"
    whatsapp_graph_api_version_secret_key: str = "WHATSAPP_GRAPH_API_VERSION"
    whatsapp_graph_api_version: str = "v20.0"
    log_level: str = "INFO"
    log_format: str = "{asctime} - {levelname}:{name}:{message}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
