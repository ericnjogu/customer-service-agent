from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUPPORT_", env_file=".env")

    app_name: str = "customer-support-agent"
    database_url: str | None = None
    retrieval_provider: str = "memory"
    answer_provider: str = "extractive"
    confidence_threshold: float = 0.60
    seed_knowledge: bool = True
    knowledge_path: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
