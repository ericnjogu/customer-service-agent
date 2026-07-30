from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUPPORT_", env_file=".env")

    app_name: str = "customer-support-agent"
    default_tenant_id: str = Field(default="default", min_length=1)
    database_url: str | None = None
    retrieval_provider: str = "memory"
    answer_provider: str = "extractive"
    embedding_provider: str = "local"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=64, gt=0)
    question_planner_provider: str = "rules"
    human_request_detector_provider: str = "rules"
    llm_model: str = "gpt-4.1-mini"
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    llm_temperature: float = 0.0
    confidence_threshold: float = 0.60
    conversation_history_max_messages: int = Field(default=50, gt=0)
    greeting_lapse_minutes: int = Field(default=60, gt=0)
    tenant_config_cache_provider: str = "memory"
    tenant_config_cache_ttl_seconds: int = Field(default=300, gt=0)
    redis_url: str | None = None
    vector_collection: str = Field(default="customer-support", min_length=1)
    knowledge_object_store_provider: str = "memory"
    knowledge_object_store_bucket: str = "customer-support-knowledge"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region_name: str = "us-east-1"
    s3_secure: bool = False
    knowledge_ingestion_queue_provider: str = "memory"
    knowledge_ingestion_queue_name: str = "knowledge-ingestion-jobs"
    knowledge_ingestion_worker_poll_seconds: int = Field(default=5, gt=0)
    telegram_bot_token: str | None = None
    telegram_webhook_secret_token: str | None = None
    telegram_credential_provider: str = "static"
    telegram_secret_namespace: str | None = None
    telegram_bot_token_secret_key: str = "TELEGRAM_BOT_TOKEN"
    telegram_webhook_secret_token_secret_key: str = "TELEGRAM_WEBHOOK_SECRET_TOKEN"
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_graph_api_version: str = "v20.0"
    seed_knowledge: bool = True
    knowledge_path: str | None = None
    knowledge_chunk_size: int = Field(default=1_200, gt=0)
    knowledge_chunk_overlap: int = Field(default=200, ge=0)
    knowledge_pdf_ocr_provider: str = "none"
    knowledge_pdf_ocr_model: str | None = None
    knowledge_pdf_ocr_dpi: int = Field(default=120, gt=0)
    log_level: str = "INFO"
    log_format: str = "{asctime} - {levelname}:{name}:{message}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
