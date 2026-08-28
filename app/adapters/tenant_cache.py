import logging
from typing import Any

from app.models import TenantConfig, TenantPlan
from app.ports import TenantConfigRepository
from app.tenancy import normalize_tenant_id

logger = logging.getLogger(__name__)


class MemoryCachedTenantConfigRepository:
    def __init__(self, inner: TenantConfigRepository) -> None:
        self.inner = inner
        self.cache: dict[str, TenantConfig] = {}

    async def initialize(self) -> None:
        await self.inner.initialize()

    async def get(self, tenant_id: str) -> TenantConfig:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        if normalized_tenant_id not in self.cache:
            self.cache[normalized_tenant_id] = await self.inner.get(normalized_tenant_id)
        return self.cache[normalized_tenant_id]

    async def get_existing(self, tenant_id: str) -> TenantConfig | None:
        tenant_config = await self.inner.get_existing(tenant_id)
        if tenant_config is not None:
            self.cache[tenant_config.tenant_id] = tenant_config
        return tenant_config

    async def upsert(
        self,
        tenant_id: str,
        *,
        selected_plan: TenantPlan | None = None,
        enabled_features: list[str] | None = None,
        answer_prompt_instructions: str | None = None,
        planner_prompt_instructions: str | None = None,
        llm_project_id: str | None = None,
        llm_project_name: str | None = None,
        langsmith_project: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        vector_provider: str | None = None,
        vector_isolation_mode: str | None = None,
        vector_collection: str | None = None,
        vector_namespace: str | None = None,
        telegram_secret_name: str | None = None,
        whatsapp_secret_name: str | None = None,
        web_search_provider: str | None = None,
        web_search_project_name: str | None = None,
    ) -> TenantConfig:
        updated = await self.inner.upsert(
            tenant_id,
            selected_plan=selected_plan,
            enabled_features=enabled_features,
            answer_prompt_instructions=answer_prompt_instructions,
            planner_prompt_instructions=planner_prompt_instructions,
            llm_project_id=llm_project_id,
            llm_project_name=llm_project_name,
            langsmith_project=langsmith_project,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            vector_provider=vector_provider,
            vector_isolation_mode=vector_isolation_mode,
            vector_collection=vector_collection,
            vector_namespace=vector_namespace,
            telegram_secret_name=telegram_secret_name,
            whatsapp_secret_name=whatsapp_secret_name,
            web_search_provider=web_search_provider,
            web_search_project_name=web_search_project_name,
        )
        self.cache[updated.tenant_id] = updated
        return updated


class RedisTenantConfigRepository:
    def __init__(
        self,
        inner: TenantConfigRepository,
        redis_client: Any,
        *,
        ttl_seconds: int = 300,
        key_prefix: str = "tenant-config",
    ) -> None:
        self.inner = inner
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix

    async def initialize(self) -> None:
        await self.inner.initialize()

    async def close(self) -> None:
        close = getattr(self.redis, "aclose", None)
        if close:
            await close()

    async def get(self, tenant_id: str) -> TenantConfig:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        cached = await self._get_cached(normalized_tenant_id)
        if cached is not None:
            return cached

        tenant_config = await self.inner.get(normalized_tenant_id)
        await self._set_cached(tenant_config)
        return tenant_config

    async def get_existing(self, tenant_id: str) -> TenantConfig | None:
        tenant_config = await self.inner.get_existing(tenant_id)
        if tenant_config is not None:
            await self._set_cached(tenant_config)
        return tenant_config

    async def upsert(
        self,
        tenant_id: str,
        *,
        selected_plan: TenantPlan | None = None,
        enabled_features: list[str] | None = None,
        answer_prompt_instructions: str | None = None,
        planner_prompt_instructions: str | None = None,
        llm_project_id: str | None = None,
        llm_project_name: str | None = None,
        langsmith_project: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        vector_provider: str | None = None,
        vector_isolation_mode: str | None = None,
        vector_collection: str | None = None,
        vector_namespace: str | None = None,
        telegram_secret_name: str | None = None,
        whatsapp_secret_name: str | None = None,
        web_search_provider: str | None = None,
        web_search_project_name: str | None = None,
    ) -> TenantConfig:
        updated = await self.inner.upsert(
            tenant_id,
            selected_plan=selected_plan,
            enabled_features=enabled_features,
            answer_prompt_instructions=answer_prompt_instructions,
            planner_prompt_instructions=planner_prompt_instructions,
            llm_project_id=llm_project_id,
            llm_project_name=llm_project_name,
            langsmith_project=langsmith_project,
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            vector_provider=vector_provider,
            vector_isolation_mode=vector_isolation_mode,
            vector_collection=vector_collection,
            vector_namespace=vector_namespace,
            telegram_secret_name=telegram_secret_name,
            whatsapp_secret_name=whatsapp_secret_name,
            web_search_provider=web_search_provider,
            web_search_project_name=web_search_project_name,
        )
        await self._set_cached(updated)
        return updated

    def _key(self, tenant_id: str) -> str:
        return f"{self.key_prefix}:{tenant_id}"

    async def _get_cached(self, tenant_id: str) -> TenantConfig | None:
        try:
            raw = await self.redis.get(self._key(tenant_id))
        except Exception:
            logger.warning("Could not read tenant config from Redis", exc_info=True)
            return None

        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return TenantConfig.model_validate_json(raw)

    async def _set_cached(self, tenant_config: TenantConfig) -> None:
        try:
            await self.redis.set(
                self._key(tenant_config.tenant_id),
                tenant_config.model_dump_json(),
                ex=self.ttl_seconds,
            )
        except Exception:
            logger.warning("Could not write tenant config to Redis", exc_info=True)


def create_redis_client(redis_url: str) -> Any:
    from redis.asyncio import Redis

    return Redis.from_url(redis_url, decode_responses=True)
