from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUPPORT_", env_file=".env")

    app_name: str = "customer-support-agent"
    database_url: str | None = None
    retrieval_provider: str = "memory"
    answer_provider: str = "extractive"
    embedding_provider: str = "local"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=64, gt=0)
    human_request_detector_provider: str = "rules"
    llm_model: str = "gpt-4.1-mini"
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    llm_temperature: float = 0.0
    confidence_threshold: float = 0.60
    conversation_history_max_messages: int = Field(default=50, gt=0)
    greeting_lapse_minutes: int = Field(default=60, gt=0)
    telegram_bot_token: str | None = None
    telegram_webhook_secret_token: str | None = None
    seed_knowledge: bool = True
    knowledge_path: str | None = None
    knowledge_chunk_size: int = Field(default=1_200, gt=0)
    knowledge_chunk_overlap: int = Field(default=200, ge=0)
    log_level: str = "INFO"
    log_format: str = "{asctime} - {levelname}:{name}:{message}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
