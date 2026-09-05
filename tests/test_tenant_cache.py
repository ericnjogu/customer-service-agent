import pytest

from app.adapters.tenant_cache import (
    ElastiCacheIAMCredentialProvider,
    MemoryCachedTenantConfigRepository,
    RedisTenantConfigRepository,
)
from app.models import TenantConfig, TenantPlan


class RecordingTenantConfigRepository:
    def __init__(self) -> None:
        self.configs: dict[str, TenantConfig] = {}
        self.get_calls = 0
        self.upsert_calls = 0

    async def initialize(self) -> None:
        return None

    async def get(self, tenant_id: str) -> TenantConfig:
        self.get_calls += 1
        return self.configs.get(tenant_id, TenantConfig.with_defaults(tenant_id))

    async def get_existing(self, tenant_id: str) -> TenantConfig | None:
        return self.configs.get(tenant_id)

    async def upsert(
        self,
        tenant_id: str,
        *,
        selected_plan: TenantPlan | None = None,
        enabled_features: list[str] | None = None,
        business_summary: str | None = None,
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
        self.upsert_calls += 1
        existing = await self.get(tenant_id)
        updated = existing.model_copy(
            update={
                "selected_plan": selected_plan or existing.selected_plan,
                "enabled_features": (
                    enabled_features
                    if enabled_features is not None
                    else existing.enabled_features
                ),
                "business_summary": (
                    business_summary
                    if business_summary is not None
                    else existing.business_summary
                ),
            }
        )
        self.configs[updated.tenant_id] = updated
        return updated


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.get_calls = 0
        self.set_calls = 0
        self.closed = False

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        self.set_calls += 1
        self.values[key] = value

    async def aclose(self) -> None:
        self.closed = True


class FailingRedis(FakeRedis):
    async def get(self, key: str) -> str | None:
        raise RuntimeError("redis unavailable")


class FakeBotocoreSession:
    class EventEmitter:
        pass

    def get_credentials(self) -> object:
        return object()

    def get_component(self, name: str) -> object:
        assert name == "event_emitter"
        return self.EventEmitter()


class FakeRequestSigner:
    def __init__(self) -> None:
        self.calls = 0

    def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
        self.calls += 1
        return "https://signed-token"


def test_elasticache_iam_credentials_are_cached_and_cache_name_is_lowercase() -> None:
    provider = ElastiCacheIAMCredentialProvider(
        "RISTOH-AI-CACHE",
        "risto-cache-app",
        "eu-central-1",
        session=FakeBotocoreSession(),
    )
    signer = FakeRequestSigner()
    provider._signer = signer

    assert provider.get_credentials() == ("risto-cache-app", "signed-token")
    assert provider.get_credentials() == ("risto-cache-app", "signed-token")
    assert provider.cache_name == "ristoh-ai-cache"
    assert signer.calls == 1


@pytest.mark.asyncio
async def test_elasticache_iam_credentials_support_async_clients() -> None:
    provider = ElastiCacheIAMCredentialProvider(
        "RISTOH-AI-CACHE",
        "risto-cache-app",
        "eu-central-1",
        session=FakeBotocoreSession(),
    )
    signer = FakeRequestSigner()
    provider._signer = signer

    assert await provider.get_credentials_async() == (
        "risto-cache-app",
        "signed-token",
    )
    assert provider.get_credentials() == ("risto-cache-app", "signed-token")
    assert signer.calls == 1

    async def set(self, key: str, value: str, ex: int) -> None:
        raise RuntimeError("redis unavailable")


@pytest.mark.asyncio
async def test_memory_cached_tenant_config_repository_reads_inner_once_per_tenant() -> None:
    inner = RecordingTenantConfigRepository()
    cache = MemoryCachedTenantConfigRepository(inner)

    first = await cache.get("tenant-a")
    second = await cache.get("tenant-a")

    assert first == second
    assert inner.get_calls == 1


@pytest.mark.asyncio
async def test_memory_cached_tenant_config_repository_refreshes_cache_after_upsert() -> None:
    inner = RecordingTenantConfigRepository()
    cache = MemoryCachedTenantConfigRepository(inner)

    initial = await cache.get("tenant-a")
    updated = await cache.upsert(
        "tenant-a",
        selected_plan="enterprise",
        enabled_features=["telegram"],
        business_summary="Use tenant voice.",
    )
    cached = await cache.get("tenant-a")

    assert initial.selected_plan == "sme"
    assert updated.selected_plan == "enterprise"
    assert cached.selected_plan == "enterprise"
    assert cached.enabled_features == ["telegram"]
    assert cached.business_summary == "Use tenant voice."
    assert inner.upsert_calls == 1


@pytest.mark.asyncio
async def test_redis_tenant_config_repository_reads_inner_once_after_cache_fill() -> None:
    inner = RecordingTenantConfigRepository()
    redis = FakeRedis()
    cache = RedisTenantConfigRepository(inner, redis, ttl_seconds=60)

    first = await cache.get("tenant-a")
    second = await cache.get("tenant-a")

    assert first == second
    assert inner.get_calls == 1
    assert redis.get_calls == 2
    assert redis.set_calls == 1


@pytest.mark.asyncio
async def test_redis_tenant_config_repository_uses_descriptive_key() -> None:
    inner = RecordingTenantConfigRepository()
    redis = FakeRedis()
    cache = RedisTenantConfigRepository(inner, redis, ttl_seconds=60)

    await cache.get("tenant-a")

    assert list(redis.values) == ["tenant-config:tenant-a"]


@pytest.mark.asyncio
async def test_redis_tenant_config_repository_refreshes_cache_after_upsert() -> None:
    inner = RecordingTenantConfigRepository()
    redis = FakeRedis()
    cache = RedisTenantConfigRepository(inner, redis, ttl_seconds=60)

    updated = await cache.upsert(
        "tenant-a",
        selected_plan="enterprise",
        enabled_features=["telegram"],
        business_summary="Use tenant voice.",
    )
    cached = await cache.get("tenant-a")

    assert updated.selected_plan == "enterprise"
    assert cached.selected_plan == "enterprise"
    assert cached.enabled_features == ["telegram"]
    assert cached.business_summary == "Use tenant voice."
    assert inner.upsert_calls == 1


@pytest.mark.asyncio
async def test_redis_tenant_config_repository_falls_back_when_redis_fails() -> None:
    inner = RecordingTenantConfigRepository()
    cache = RedisTenantConfigRepository(inner, FailingRedis(), ttl_seconds=60)

    tenant_config = await cache.get("tenant-a")
    updated = await cache.upsert("tenant-a", selected_plan="enterprise")

    assert tenant_config.tenant_id == "tenant-a"
    assert updated.selected_plan == "enterprise"
    assert inner.get_calls == 2
    assert inner.upsert_calls == 1


@pytest.mark.asyncio
async def test_redis_tenant_config_repository_closes_redis_client() -> None:
    redis = FakeRedis()
    cache = RedisTenantConfigRepository(RecordingTenantConfigRepository(), redis)

    await cache.close()

    assert redis.closed is True
