import logging
from datetime import datetime, timedelta, timezone
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
        updated = await self.inner.upsert(
            tenant_id,
            selected_plan=selected_plan,
            enabled_features=enabled_features,
            business_summary=business_summary,
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
        updated = await self.inner.upsert(
            tenant_id,
            selected_plan=selected_plan,
            enabled_features=enabled_features,
            business_summary=business_summary,
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


class ElastiCacheIAMCredentialProvider:
    """Generate and briefly cache SigV4 credentials for ElastiCache IAM auth."""

    def __init__(
        self,
        cache_name: str,
        username: str,
        region: str,
        *,
        session: Any | None = None,
    ) -> None:
        from botocore.model import ServiceId
        from botocore.session import Session
        from botocore.signers import RequestSigner

        self.cache_name = cache_name.lower()
        self.username = username
        self.region = region
        self._session = session or Session()
        self._signer = RequestSigner(
            ServiceId("elasticache"),
            region,
            "elasticache",
            "v4",
            self._session.get_credentials(),
            self._session.get_component("event_emitter"),
        )
        self._token: str | None = None
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)

    def get_credentials(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        if self._token is None or now >= self._expires_at:
            url = self._signer.generate_presigned_url(
                {
                    "method": "GET",
                    "url": (
                        f"https://{self.cache_name}/"
                        f"?Action=connect&User={self.username}"
                    ),
                    "body": {},
                    "headers": {},
                    "context": {},
                },
                operation_name="connect",
                expires_in=900,
                region_name=self.region,
            )
            self._token = url.removeprefix("https://")
            self._expires_at = now + timedelta(minutes=14)
        return self.username, self._token

    async def get_credentials_async(self) -> tuple[str, str]:
        return self.get_credentials()


def create_redis_client(
    redis_url: str,
    *,
    iam_cache_name: str | None = None,
    iam_username: str | None = None,
    aws_region: str = "eu-central-1",
) -> Any:
    from redis.asyncio import Redis

    credential_provider = None
    if iam_cache_name or iam_username:
        if not iam_cache_name or not iam_username:
            raise ValueError(
                "Both IAM cache name and username are required for ElastiCache IAM auth"
            )
        credential_provider = ElastiCacheIAMCredentialProvider(
            iam_cache_name,
            iam_username,
            aws_region,
        )

    return Redis.from_url(
        redis_url,
        decode_responses=True,
        credential_provider=credential_provider,
        ssl_cert_reqs="required",
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=30,
    )
