import json
import logging
from datetime import datetime
from uuid import UUID

import asyncpg
from langchain_core.documents import Document

from app.models import (
    BusinessContactPointRecord,
    BusinessProfileRecord,
    ConversationRecord,
    IncomingMessage,
    OnboardingAdmin,
    OnboardingBusinessProfile,
    OnboardingContactPoint,
    OnboardingEmailVerificationDiagnostic,
    OnboardingJobRecord,
    OnboardingProviderProjects,
    OnboardingSessionRecord,
    OnboardingSessionUpdate,
    OnboardingTelegramSetup,
    StoredMessage,
    TenantConfig,
    TenantMembershipRecord,
    TenantPlan,
    TenantRecord,
    WebsiteAnalysisResult,
)
from app.ports import EmbeddingProvider
from app.tenancy import DEFAULT_TENANT_PLAN, generate_tenant_id, normalize_tenant_id, tenant_slug

logger = logging.getLogger(__name__)


def content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def is_zero_vector(values: list[float]) -> bool:
    return not any(values)


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


def website_domain_sql(column: str) -> str:
    return (
        "split_part("
        "split_part("
        f"regexp_replace(regexp_replace(lower({column}), '^https?://', ''), "
        "'^www\\.', ''), "
        "'/', 1), "
        "':', 1)"
    )


def decode_metadata(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    return {}


def token_fingerprint(token_hash: str | None) -> str | None:
    return token_hash[:12] if token_hash else None


def row_to_tenant_config(row: asyncpg.Record) -> TenantConfig:
    data = dict(row)
    data["enabled_features"] = [
        str(item) for item in (data.get("enabled_features") or [])
    ]
    return TenantConfig(**data)


def row_to_tenant(row: asyncpg.Record) -> TenantRecord:
    return TenantRecord(**dict(row))


def row_to_onboarding_job(row: asyncpg.Record) -> OnboardingJobRecord:
    return OnboardingJobRecord(**dict(row))


def row_to_onboarding_session(row: asyncpg.Record) -> OnboardingSessionRecord:
    data = dict(row)
    payload = decode_metadata(data.pop("session_payload"))
    return OnboardingSessionRecord(
        session_id=data["session_id"],
        status=data["status"],
        current_step=data["current_step"],
        website_url=data["website_url"],
        website_verification_email=data.get("website_verification_email"),
        admin=payload["admin"],
        terms_version=payload.get("terms_version"),
        terms_accepted_at=payload.get("terms_accepted_at"),
        username_email_verified=data["username_email_verified"],
        username_email_verification_expires_at=data[
            "username_email_verification_expires_at"
        ],
        website_email_verified=data["website_email_verified"],
        website_email_verification_expires_at=data[
            "website_email_verification_expires_at"
        ],
        analysis=payload.get("analysis"),
        business_profile=payload.get("business_profile"),
        agent_name=payload.get("agent_name"),
        agent_description=payload.get("agent_description"),
        answer_prompt_instructions=payload.get("answer_prompt_instructions"),
        contact_info=payload.get("contact_info") or payload.get("social_links") or [],
        telegram=payload.get("telegram"),
        provider_projects=payload.get("provider_projects") or {},
        knowledge_sources=payload.get("knowledge_sources") or [],
        telegram_setup_url=data["telegram_setup_url"],
        telegram_setup_token_expires_at=data["telegram_setup_token_expires_at"],
        submitted_job_id=data["submitted_job_id"],
        error=data["error"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def session_payload(session: OnboardingSessionRecord) -> dict:
    return {
        "admin": session.admin.model_dump(mode="json"),
        "terms_version": session.terms_version,
        "terms_accepted_at": model_to_json(session.terms_accepted_at),
        "analysis": model_to_json(session.analysis),
        "business_profile": model_to_json(session.business_profile),
        "agent_name": session.agent_name,
        "agent_description": session.agent_description,
        "answer_prompt_instructions": session.answer_prompt_instructions,
        "contact_info": [point.model_dump(mode="json") for point in session.contact_info],
        "telegram": model_to_json(session.telegram),
        "provider_projects": session.provider_projects.model_dump(mode="json"),
        "knowledge_sources": model_to_json(session.knowledge_sources),
    }


def model_to_json(value: object) -> object:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [model_to_json(item) for item in value]
    return value


class PostgresDatabase:
    def __init__(self, database_url: str, embedding_dimensions: int = 64) -> None:
        self.database_url = database_url
        self.embedding_dimensions = embedding_dimensions
        self.pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url)
        async with self.pool.acquire() as connection:
            await connection.execute(schema(self.embedding_dimensions))
            await self._validate_embedding_dimensions(connection)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def _validate_embedding_dimensions(self, connection: asyncpg.Connection) -> None:
        embedding_type = await connection.fetchval(
            """
            SELECT format_type(attribute.atttypid, attribute.atttypmod)
            FROM pg_attribute attribute
            JOIN pg_class relation ON relation.oid = attribute.attrelid
            WHERE relation.relname = 'knowledge_documents'
              AND attribute.attname = 'embedding'
              AND NOT attribute.attisdropped
            """
        )
        expected = f"vector({self.embedding_dimensions})"
        if embedding_type != expected:
            raise ValueError(
                "knowledge_documents.embedding has type "
                f"{embedding_type}, but configured embedding dimensions require {expected}. "
                "Use a fresh database, recreate the knowledge_documents table, or reindex "
                "the KB after changing AGENT_EMBEDDING_DIMENSIONS."
            )


class PostgresConversationRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    async def initialize(self) -> None:
        return None

    async def get_or_create(self, message: IncomingMessage) -> ConversationRecord:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            INSERT INTO conversations(tenant_id, channel, external_chat_id, external_user_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id, channel, external_chat_id) DO UPDATE
            SET external_user_id = EXCLUDED.external_user_id, updated_at = now()
            RETURNING id, tenant_id, channel, external_chat_id, external_user_id, state, created_at
            """,
            message.tenant_id,
            message.channel,
            message.external_chat_id,
            message.external_user_id,
        )
        return ConversationRecord(**dict(row))

    async def get_by_id(self, conversation_id: UUID) -> ConversationRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT id, tenant_id, channel, external_chat_id, external_user_id, state, created_at
            FROM conversations WHERE id = $1
            """,
            conversation_id,
        )
        return ConversationRecord(**dict(row)) if row else None

    async def update_state(
        self,
        conversation_id: UUID,
        *,
        state: str,
        reason: str | None = None,
    ) -> ConversationRecord:
        assert self.database.pool
        async with self.database.pool.acquire() as connection:
            async with connection.transaction():
                previous = await connection.fetchrow(
                    """
                    SELECT state FROM conversations WHERE id = $1
                    FOR UPDATE
                    """,
                    conversation_id,
                )
                if not previous:
                    raise KeyError(f"Conversation not found: {conversation_id}")

                row = await connection.fetchrow(
                    """
                    UPDATE conversations
                    SET state = $2, updated_at = now()
                    WHERE id = $1
                    RETURNING
                        id,
                        tenant_id,
                        channel,
                        external_chat_id,
                        external_user_id,
                        state,
                        created_at
                    """,
                    conversation_id,
                    state,
                )
                await connection.execute(
                    """
                    INSERT INTO conversation_state_events(
                        conversation_id,
                        previous_state,
                        new_state,
                        reason
                    )
                    VALUES ($1, $2, $3, $4)
                    """,
                    conversation_id,
                    previous["state"],
                    row["state"],
                    reason,
                )
        return ConversationRecord(**dict(row))

    async def save_message(self, message: StoredMessage) -> bool:
        assert self.database.pool
        result = await self.database.pool.execute(
            """
            INSERT INTO messages(
                tenant_id,
                conversation_id,
                event_id,
                sender_type,
                body,
                in_scope,
                created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (tenant_id, event_id) DO NOTHING
            """,
            message.tenant_id,
            message.conversation_id,
            message.event_id,
            message.sender_type,
            message.body,
            message.in_scope,
            message.created_at,
        )
        return result == "INSERT 0 1"

    async def list_messages_since(
        self,
        conversation_id: UUID,
        since: datetime,
        limit: int,
    ) -> list[StoredMessage]:
        assert self.database.pool
        rows = await self.database.pool.fetch(
            """
            SELECT tenant_id, conversation_id, event_id, sender_type, body, in_scope, created_at
            FROM messages
            WHERE conversation_id = $1 AND created_at >= $2 AND in_scope = true
            ORDER BY created_at DESC
            LIMIT $3
            """,
            conversation_id,
            since,
            limit,
        )
        return [StoredMessage(**dict(row)) for row in reversed(rows)]

    async def minutes_since_previous_customer_message(
        self,
        conversation_id: UUID,
        current_event_id: str,
        current_received_at: datetime,
    ) -> int | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT GREATEST(
                0,
                FLOOR(EXTRACT(EPOCH FROM ($3::timestamptz - created_at)) / 60)::int
            ) AS minutes
            FROM messages
            WHERE conversation_id = $1
              AND sender_type = 'CUSTOMER'
              AND event_id <> $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            conversation_id,
            current_event_id,
            current_received_at,
        )
        return int(row["minutes"]) if row else None


class PgVectorRetrievalStore:
    def __init__(self, database: PostgresDatabase, embeddings: EmbeddingProvider) -> None:
        self.database = database
        self.embeddings = embeddings

    async def initialize(self) -> None:
        return None

    async def upsert(self, documents: list[Document], namespace: str) -> None:
        assert self.database.pool
        logger.info(
            "Upserting %d pgvector knowledge document(s) into namespace=%s",
            len(documents),
            namespace,
        )
        async with self.database.pool.acquire() as connection:
            pending: list[tuple[Document, str, str]] = []
            incoming_chunk_ids_by_source: dict[str, set[str]] = {}
            for document in documents:
                source = str(document.metadata.get("source", "unknown"))
                chunk_id = str(document.metadata.get("chunk_id", source))
                incoming_chunk_ids_by_source.setdefault(source, set()).add(chunk_id)
                document_hash = content_hash(document.page_content)
                existing_hash = await connection.fetchval(
                    """
                    SELECT content_hash FROM knowledge_documents
                    WHERE namespace = $1 AND chunk_id = $2
                    """,
                    namespace,
                    chunk_id,
                )
                if existing_hash == document_hash:
                    logger.info(
                        "Skipping unchanged pgvector knowledge chunk namespace=%s chunk_id=%s",
                        namespace,
                        chunk_id,
                    )
                    continue
                logger.info(
                    "Preparing pgvector knowledge chunk namespace=%s chunk_id=%s content_chars=%d",
                    namespace,
                    chunk_id,
                    len(document.page_content),
                )
                pending.append((document, chunk_id, document_hash))

            if not pending:
                logger.info("No changed pgvector knowledge documents for namespace=%s", namespace)
                await self._delete_removed_chunks(
                    connection,
                    namespace,
                    incoming_chunk_ids_by_source,
                )
                return

            embeddings = await self.embeddings.embed_documents(
                [document.page_content for document, _, _ in pending]
            )
            for (document, chunk_id, document_hash), embedding in zip(
                pending,
                embeddings,
                strict=True,
            ):
                source = str(document.metadata.get("source", "unknown"))
                source_url = document.metadata.get("source_url")
                await connection.execute(
                    """
                    INSERT INTO knowledge_documents(
                        namespace,
                        chunk_id,
                        source,
                        source_url,
                        content,
                        content_hash,
                        metadata,
                        embedding
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::vector)
                    ON CONFLICT (namespace, chunk_id) DO UPDATE
                    SET content = EXCLUDED.content,
                        source = EXCLUDED.source,
                        source_url = EXCLUDED.source_url,
                        content_hash = EXCLUDED.content_hash,
                        metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding,
                        updated_at = now()
                    """,
                    namespace,
                    chunk_id,
                    source,
                    str(source_url) if source_url else None,
                    document.page_content,
                    document_hash,
                    json.dumps(document.metadata),
                    vector_literal(embedding),
                )
            await self._delete_removed_chunks(
                connection,
                namespace,
                incoming_chunk_ids_by_source,
            )
        logger.info("Finished pgvector knowledge upsert for namespace=%s", namespace)

    async def delete_by_metadata(
        self,
        namespace: str,
        metadata_key: str,
        metadata_value: str,
    ) -> None:
        assert self.database.pool
        result = await self.database.pool.execute(
            """
            DELETE FROM knowledge_documents
            WHERE namespace = $1
              AND metadata ->> $2 = $3
            """,
            namespace,
            metadata_key,
            metadata_value,
        )
        logger.info(
            "Deleted pgvector knowledge documents by metadata namespace=%s "
            "metadata_key=%s metadata_value=%s result=%s",
            namespace,
            metadata_key,
            metadata_value,
            result,
        )

    async def _delete_removed_chunks(
        self,
        connection: asyncpg.Connection,
        namespace: str,
        incoming_chunk_ids_by_source: dict[str, set[str]],
    ) -> None:
        for source, chunk_ids in incoming_chunk_ids_by_source.items():
            await connection.execute(
                """
                DELETE FROM knowledge_documents
                WHERE namespace = $1
                  AND source = $2
                  AND NOT (chunk_id = ANY($3::text[]))
                """,
                namespace,
                source,
                sorted(chunk_ids),
            )

    async def search(self, query: str, namespace: str, limit: int = 4) -> list[Document]:
        assert self.database.pool
        query_embedding = await self.embeddings.embed_query(query)
        if is_zero_vector(query_embedding):
            logger.debug(
                "Skipping pgvector search for namespace=%s because query has no embedding tokens",
                namespace,
            )
            return []

        rows = await self.database.pool.fetch(
            """
            SELECT content, source_url, metadata, created_at,
                   1 - (embedding <=> $1::vector) AS score
            FROM knowledge_documents
            WHERE namespace = $2
            ORDER BY embedding <=> $1::vector LIMIT $3
            """,
            vector_literal(query_embedding),
            namespace,
            limit,
        )
        return [
            Document(
                page_content=row["content"],
                metadata={
                    **decode_metadata(row["metadata"]),
                    "source_url": row["source_url"],
                    "created_at": row["created_at"].isoformat(),
                    "score": row["score"],
                },
            )
            for row in rows
            if row["score"] > 0
        ]


class PostgresTenantConfigRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        default_vector_collection: str = "customer-service",
    ) -> None:
        self.database = database
        self.default_vector_collection = default_vector_collection

    async def initialize(self) -> None:
        return None

    async def get(self, tenant_id: str) -> TenantConfig:
        existing = await self.get_existing(tenant_id)
        if existing is not None:
            return existing
        return TenantConfig.with_defaults(
            normalize_tenant_id(tenant_id),
            vector_collection=self.default_vector_collection,
        )

    async def get_existing(self, tenant_id: str) -> TenantConfig | None:
        assert self.database.pool
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        row = await self.database.pool.fetchrow(
            """
            SELECT
                tenant_configs.tenant_id,
                tenant_configs.selected_plan,
                COALESCE(
                    array_agg(
                        tenant_enabled_features.feature_key
                        ORDER BY tenant_enabled_features.feature_key
                    )
                    FILTER (WHERE tenant_enabled_features.feature_key IS NOT NULL),
                    ARRAY[]::text[]
                ) AS enabled_features,
                tenant_configs.answer_prompt_instructions,
                tenant_configs.planner_prompt_instructions,
                tenant_configs.llm_project_id,
                tenant_configs.llm_project_name,
                tenant_configs.langsmith_project,
                tenant_configs.llm_provider,
                tenant_configs.llm_model,
                tenant_configs.llm_base_url,
                tenant_configs.vector_provider,
                tenant_configs.vector_isolation_mode,
                tenant_configs.vector_collection,
                tenant_configs.vector_namespace,
                tenant_configs.telegram_secret_name,
                tenant_configs.whatsapp_secret_name,
                tenant_configs.web_search_provider,
                tenant_configs.web_search_project_name,
                tenant_configs.created_at,
                tenant_configs.updated_at
            FROM tenant_configs
            LEFT JOIN tenant_enabled_features
                ON tenant_enabled_features.tenant_id = tenant_configs.tenant_id
            WHERE tenant_configs.tenant_id = $1
            GROUP BY tenant_configs.tenant_id
            """,
            normalized_tenant_id,
        )
        if row:
            return row_to_tenant_config(row)
        return None

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
        assert self.database.pool
        existing = await self.get(tenant_id)
        normalized_tenant_id = existing.tenant_id
        await self.database.pool.execute(
            """
            INSERT INTO tenant_configs(
                tenant_id,
                selected_plan,
                answer_prompt_instructions,
                planner_prompt_instructions,
                llm_project_id,
                llm_project_name,
                langsmith_project,
                llm_provider,
                llm_model,
                llm_base_url,
                vector_provider,
                vector_isolation_mode,
                vector_collection,
                vector_namespace,
                telegram_secret_name,
                whatsapp_secret_name,
                web_search_provider,
                web_search_project_name
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8,
                $9, $10, $11, $12, $13, $14, $15, $16,
                $17, $18
            )
            ON CONFLICT (tenant_id) DO UPDATE
            SET selected_plan = EXCLUDED.selected_plan,
                answer_prompt_instructions = EXCLUDED.answer_prompt_instructions,
                planner_prompt_instructions = EXCLUDED.planner_prompt_instructions,
                llm_project_id = EXCLUDED.llm_project_id,
                llm_project_name = EXCLUDED.llm_project_name,
                langsmith_project = EXCLUDED.langsmith_project,
                llm_provider = EXCLUDED.llm_provider,
                llm_model = EXCLUDED.llm_model,
                llm_base_url = EXCLUDED.llm_base_url,
                vector_provider = EXCLUDED.vector_provider,
                vector_isolation_mode = EXCLUDED.vector_isolation_mode,
                vector_collection = EXCLUDED.vector_collection,
                vector_namespace = EXCLUDED.vector_namespace,
                telegram_secret_name = EXCLUDED.telegram_secret_name,
                whatsapp_secret_name = EXCLUDED.whatsapp_secret_name,
                web_search_provider = EXCLUDED.web_search_provider,
                web_search_project_name = EXCLUDED.web_search_project_name,
                updated_at = now()
            """,
            normalized_tenant_id,
            selected_plan or existing.selected_plan,
            answer_prompt_instructions
            if answer_prompt_instructions is not None
            else existing.answer_prompt_instructions,
            planner_prompt_instructions
            if planner_prompt_instructions is not None
            else existing.planner_prompt_instructions,
            llm_project_id if llm_project_id is not None else existing.llm_project_id,
            llm_project_name
            if llm_project_name is not None
            else existing.llm_project_name,
            langsmith_project if langsmith_project is not None else existing.langsmith_project,
            llm_provider if llm_provider is not None else existing.llm_provider,
            llm_model if llm_model is not None else existing.llm_model,
            llm_base_url if llm_base_url is not None else existing.llm_base_url,
            vector_provider if vector_provider is not None else existing.vector_provider,
            vector_isolation_mode
            if vector_isolation_mode is not None
            else existing.vector_isolation_mode,
            vector_collection if vector_collection is not None else existing.vector_collection,
            vector_namespace
            if vector_namespace is not None
            else existing.vector_namespace,
            telegram_secret_name
            if telegram_secret_name is not None
            else existing.telegram_secret_name,
            whatsapp_secret_name
            if whatsapp_secret_name is not None
            else existing.whatsapp_secret_name,
            web_search_provider
            if web_search_provider is not None
            else existing.web_search_provider,
            web_search_project_name
            if web_search_project_name is not None
            else existing.web_search_project_name,
        )
        if enabled_features is not None:
            await self.database.pool.execute(
                """
                DELETE FROM tenant_enabled_features
                WHERE tenant_id = $1
                """,
                normalized_tenant_id,
            )
            await self.database.pool.executemany(
                """
                INSERT INTO tenant_enabled_features(tenant_id, feature_key)
                VALUES ($1, $2)
                ON CONFLICT (tenant_id, feature_key) DO NOTHING
                """,
                [
                    (normalized_tenant_id, feature_key)
                    for feature_key in sorted(set(enabled_features))
                ],
            )
        return await self.get(normalized_tenant_id)


class PostgresTenantRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    async def initialize(self) -> None:
        return None

    async def get(self, tenant_id: str) -> TenantRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT tenant_id, slug, display_name, selected_plan, created_at, updated_at
            FROM tenants
            WHERE tenant_id = $1
            """,
            normalize_tenant_id(tenant_id),
        )
        if not row:
            return None
        return row_to_tenant(row)

    async def get_by_slug(self, slug: str) -> TenantRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT tenant_id, slug, display_name, selected_plan, created_at, updated_at
            FROM tenants
            WHERE slug = $1
            """,
            tenant_slug(slug),
        )
        if not row:
            return None
        return row_to_tenant(row)

    async def create(
        self,
        *,
        display_name: str,
        slug: str | None = None,
        selected_plan: TenantPlan | None = None,
    ) -> TenantRecord:
        assert self.database.pool
        candidate_slug = tenant_slug(slug or display_name)
        plan = selected_plan or DEFAULT_TENANT_PLAN
        for _attempt in range(1, 20):
            tenant_id = generate_tenant_id()
            try:
                row = await self.database.pool.fetchrow(
                    """
                    INSERT INTO tenants(tenant_id, slug, display_name, selected_plan)
                    VALUES ($1, $2, $3, $4)
                    RETURNING tenant_id, slug, display_name, selected_plan, created_at, updated_at
                    """,
                    tenant_id,
                    candidate_slug,
                    display_name.strip(),
                    plan,
                )
                return row_to_tenant(row)
            except asyncpg.UniqueViolationError:
                logger.info(
                    "Tenant id or slug collision while creating tenant; "
                    "retrying tenant_id for slug=%s",
                    candidate_slug,
                )
        raise RuntimeError("Could not generate a unique tenant id and slug")


class PostgresOnboardingRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    async def initialize(self) -> None:
        return None

    async def create_job(
        self,
        *,
        idempotency_key: str,
        request_payload: dict,
    ) -> OnboardingJobRecord:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            INSERT INTO onboarding_jobs(idempotency_key, request_payload)
            VALUES ($1, $2::jsonb)
            ON CONFLICT (idempotency_key) DO UPDATE
            SET idempotency_key = EXCLUDED.idempotency_key
            RETURNING job_id, idempotency_key, status, tenant_id, tenant_slug, error,
                      created_at, updated_at
            """,
            idempotency_key,
            json.dumps(request_payload),
        )
        return row_to_onboarding_job(row)

    async def get_job(self, job_id: UUID) -> OnboardingJobRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT job_id, idempotency_key, status, tenant_id, tenant_slug, error,
                   created_at, updated_at
            FROM onboarding_jobs
            WHERE job_id = $1
            """,
            job_id,
        )
        return row_to_onboarding_job(row) if row else None

    async def get_job_payload(self, job_id: UUID) -> dict | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT request_payload
            FROM onboarding_jobs
            WHERE job_id = $1
            """,
            job_id,
        )
        if not row:
            return None
        return decode_metadata(row["request_payload"])

    async def get_job_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> OnboardingJobRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT job_id, idempotency_key, status, tenant_id, tenant_slug, error,
                   created_at, updated_at
            FROM onboarding_jobs
            WHERE idempotency_key = $1
            """,
            idempotency_key,
        )
        return row_to_onboarding_job(row) if row else None

    async def mark_job_accepted(self, job_id: UUID) -> OnboardingJobRecord:
        return await self._update_job(
            job_id,
            status="accepted",
            error=None,
        )

    async def mark_job_running(self, job_id: UUID) -> OnboardingJobRecord:
        return await self._update_job(job_id, status="running", error=None)

    async def mark_job_succeeded(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        tenant_slug: str,
    ) -> OnboardingJobRecord:
        return await self._update_job(
            job_id,
            status="succeeded",
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            error=None,
        )

    async def mark_job_failed(
        self,
        job_id: UUID,
        *,
        error: str,
        tenant_id: str | None = None,
        tenant_slug: str | None = None,
    ) -> OnboardingJobRecord:
        return await self._update_job(
            job_id,
            status="failed",
            error=error,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
        )

    async def save_business_profile(
        self,
        tenant_id: str,
        profile: OnboardingBusinessProfile,
    ) -> BusinessProfileRecord:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            INSERT INTO business_profiles(
                tenant_id,
                business_name,
                website_url,
                location_name,
                physical_location,
                business_phone,
                business_email,
                google_place_url
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (tenant_id) DO UPDATE
            SET business_name = EXCLUDED.business_name,
                website_url = EXCLUDED.website_url,
                location_name = EXCLUDED.location_name,
                physical_location = EXCLUDED.physical_location,
                business_phone = EXCLUDED.business_phone,
                business_email = EXCLUDED.business_email,
                google_place_url = EXCLUDED.google_place_url,
                updated_at = now()
            RETURNING tenant_id, business_name, website_url, location_name,
                      physical_location, business_phone, business_email,
                      google_place_url, created_at, updated_at
            """,
            tenant_id,
            profile.business_name,
            str(profile.website_url),
            profile.location_name,
            profile.physical_location,
            profile.business_phone,
            profile.business_email,
            str(profile.google_place_url) if profile.google_place_url else None,
        )
        return BusinessProfileRecord(**dict(row))

    async def replace_contact_points(
        self,
        tenant_id: str,
        contact_points: list[OnboardingContactPoint],
    ) -> list[BusinessContactPointRecord]:
        assert self.database.pool
        async with self.database.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM business_contact_points WHERE tenant_id = $1",
                    tenant_id,
                )
                rows = []
                for point in contact_points:
                    row = await connection.fetchrow(
                        """
                        INSERT INTO business_contact_points(
                            tenant_id, kind, label, value, url, is_primary
                        )
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING tenant_id, kind, label, value, url, is_primary, created_at
                        """,
                        tenant_id,
                        point.kind,
                        point.label,
                        point.value,
                        str(point.url) if point.url else None,
                        point.is_primary,
                    )
                    rows.append(row)
        return [BusinessContactPointRecord(**dict(row)) for row in rows]

    async def save_owner_membership(
        self,
        tenant_id: str,
        admin: OnboardingAdmin,
    ) -> TenantMembershipRecord:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            INSERT INTO tenant_memberships(tenant_id, user_email, user_name, role)
            VALUES ($1, $2, $3, 'owner')
            ON CONFLICT (tenant_id, user_email) DO UPDATE
            SET user_name = EXCLUDED.user_name,
                role = EXCLUDED.role
            RETURNING tenant_id, user_email, user_name, role, created_at
            """,
            tenant_id,
            admin.username_email.lower(),
            admin.name,
        )
        return TenantMembershipRecord(**dict(row))

    async def create_session(
        self,
        *,
        admin: OnboardingAdmin,
        terms_version: str,
        terms_accepted_at: datetime,
    ) -> OnboardingSessionRecord:
        assert self.database.pool
        payload = {
            "admin": admin.model_dump(mode="json"),
            "terms_version": terms_version,
            "terms_accepted_at": terms_accepted_at.isoformat(),
        }
        row = await self.database.pool.fetchrow(
            """
            INSERT INTO onboarding_sessions(admin_email, session_payload)
            VALUES ($1, $2::jsonb)
            RETURNING session_id, status, current_step, website_url, admin_email,
                      website_verification_email, session_payload,
                      username_email_verified, username_email_verification_expires_at,
                      website_email_verified, website_email_verification_expires_at,
                      telegram_setup_url,
                      telegram_setup_token_expires_at, submitted_job_id, error,
                      created_at, updated_at
            """,
            admin.username_email.lower(),
            json.dumps(payload),
        )
        return row_to_onboarding_session(row)

    async def save_session_website(
        self,
        session_id: UUID,
        *,
        website_url: str,
        website_verification_email: str,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        return await self._update_session_payload(
            session_id,
            status="website_verification_pending",
            current_step="website-email-verification",
            payload=session_payload(session),
            website_url=website_url,
            website_verification_email=website_verification_email.lower(),
            website_email_verified=False,
            clear_website_email_verification_token_used_at=True,
        )

    async def get_active_session_by_website_domain(
        self,
        website_domain: str,
    ) -> OnboardingSessionRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            f"""
            SELECT session_id, status, current_step, website_url, admin_email,
                   website_verification_email, session_payload,
                   username_email_verified, username_email_verification_expires_at,
                   website_email_verified, website_email_verification_expires_at,
                   telegram_setup_url,
                   telegram_setup_token_expires_at, submitted_job_id, error,
                   created_at, updated_at
            FROM onboarding_sessions
            WHERE status <> 'failed'
              AND {website_domain_sql('website_url')} = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            website_domain,
        )
        return row_to_onboarding_session(row) if row else None

    async def get_business_profile_by_website_domain(
        self,
        website_domain: str,
    ) -> BusinessProfileRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            f"""
            SELECT tenant_id, business_name, website_url, location_name,
                   physical_location, business_phone, business_email,
                   google_place_url, created_at, updated_at
            FROM business_profiles
            WHERE {website_domain_sql('website_url')} = $1
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            website_domain,
        )
        return BusinessProfileRecord(**dict(row)) if row else None

    async def get_session(self, session_id: UUID) -> OnboardingSessionRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT session_id, status, current_step, website_url, admin_email,
                   website_verification_email, session_payload,
                   username_email_verified, username_email_verification_expires_at,
                   website_email_verified, website_email_verification_expires_at,
                   telegram_setup_url,
                   telegram_setup_token_expires_at, submitted_job_id, error,
                   created_at, updated_at
            FROM onboarding_sessions
            WHERE session_id = $1
            """,
            session_id,
        )
        return row_to_onboarding_session(row) if row else None

    async def update_session(
        self,
        session_id: UUID,
        update: OnboardingSessionUpdate,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        current_step = update.current_step or session.current_step
        payload = session_payload(session)
        for field in update.model_fields_set:
            if field == "current_step":
                continue
            value = getattr(update, field)
            payload[field] = model_to_json(value)
        return await self._update_session_payload(
            session_id,
            status=session.status,
            current_step=current_step,
            payload=payload,
        )

    async def save_session_analysis(
        self,
        session_id: UUID,
        *,
        analysis: WebsiteAnalysisResult,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        payload = session_payload(session)
        payload.update(
            {
                "analysis": analysis.model_dump(mode="json"),
                "business_profile": analysis.business_profile.model_dump(mode="json"),
                "agent_name": analysis.agent_name,
                "agent_description": analysis.agent_description,
                "answer_prompt_instructions": analysis.answer_prompt_instructions,
                "contact_info": [
                    point.model_dump(mode="json") for point in analysis.contact_info
                ],
                "knowledge_sources": [
                    source.model_dump(mode="json")
                    for source in analysis.knowledge_sources
                ],
            }
        )
        return await self._update_session_payload(
            session_id,
            status="ready_for_review",
            current_step="analysis",
            payload=payload,
        )

    async def save_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        return await self._update_session_payload(
            session_id,
            status="username_email_verification_pending",
            current_step="username-email-verification",
            payload=session_payload(session),
            username_email_verified=False,
            username_email_verification_token_hash=token_hash,
            username_email_verification_expires_at=expires_at,
            clear_username_email_verification_token_used_at=True,
        )

    async def consume_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            UPDATE onboarding_sessions
            SET username_email_verified = true,
                username_email_verification_token_used_at = now(),
                status = 'draft',
                current_step = 'website',
                updated_at = now()
            WHERE session_id = $1
              AND username_email_verification_token_hash = $2
              AND username_email_verification_token_used_at IS NULL
              AND username_email_verification_expires_at > now()
            RETURNING session_id
            """,
            session_id,
            token_hash,
        )
        return row is not None

    async def inspect_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> OnboardingEmailVerificationDiagnostic:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT username_email_verified AS email_verified,
                   username_email_verification_token_hash AS token_hash,
                   username_email_verification_expires_at AS expires_at,
                   username_email_verification_token_used_at AS used_at,
                   username_email_verification_expires_at <= now() AS token_expired
            FROM onboarding_sessions
            WHERE session_id = $1
            """,
            session_id,
        )
        if row is None:
            return OnboardingEmailVerificationDiagnostic(session_exists=False)

        stored_token_hash = row["token_hash"]
        used_at = row["used_at"]
        expires_at = row["expires_at"]
        return OnboardingEmailVerificationDiagnostic(
            session_exists=True,
            admin_email_verified=row["email_verified"],
            has_token_hash=stored_token_hash is not None,
            token_matches=stored_token_hash == token_hash,
            token_used=used_at is not None,
            token_expired=bool(row["token_expired"]) if expires_at else False,
            expires_at=expires_at,
            used_at=used_at,
            submitted_token_fingerprint=token_fingerprint(token_hash),
            stored_token_fingerprint=token_fingerprint(stored_token_hash),
        )

    async def save_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        return await self._update_session_payload(
            session_id,
            status="website_verification_pending",
            current_step="website-email-verification",
            payload=session_payload(session),
            website_email_verified=False,
            website_email_verification_token_hash=token_hash,
            website_email_verification_expires_at=expires_at,
            clear_website_email_verification_token_used_at=True,
        )

    async def consume_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            UPDATE onboarding_sessions
            SET website_email_verified = true,
                website_email_verification_token_used_at = now(),
                status = 'draft',
                current_step = 'analyzing',
                updated_at = now()
            WHERE session_id = $1
              AND website_email_verification_token_hash = $2
              AND website_email_verification_token_used_at IS NULL
              AND website_email_verification_expires_at > now()
            RETURNING session_id
            """,
            session_id,
            token_hash,
        )
        return row is not None

    async def inspect_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> OnboardingEmailVerificationDiagnostic:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT website_email_verified AS email_verified,
                   website_email_verification_token_hash AS token_hash,
                   website_email_verification_expires_at AS expires_at,
                   website_email_verification_token_used_at AS used_at,
                   website_email_verification_expires_at <= now() AS token_expired
            FROM onboarding_sessions
            WHERE session_id = $1
            """,
            session_id,
        )
        if row is None:
            return OnboardingEmailVerificationDiagnostic(session_exists=False)

        stored_token_hash = row["token_hash"]
        used_at = row["used_at"]
        expires_at = row["expires_at"]
        return OnboardingEmailVerificationDiagnostic(
            session_exists=True,
            admin_email_verified=row["email_verified"],
            has_token_hash=stored_token_hash is not None,
            token_matches=stored_token_hash == token_hash,
            token_used=used_at is not None,
            token_expired=bool(row["token_expired"]) if expires_at else False,
            expires_at=expires_at,
            used_at=used_at,
            submitted_token_fingerprint=token_fingerprint(token_hash),
            stored_token_fingerprint=token_fingerprint(stored_token_hash),
        )

    async def save_telegram_setup_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        setup_url: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        return await self._update_session_payload(
            session_id,
            status="awaiting_telegram_setup",
            current_step="telegram-setup",
            payload=session_payload(session),
            telegram_setup_url=setup_url,
            telegram_setup_token_hash=token_hash,
            telegram_setup_token_expires_at=expires_at,
            clear_telegram_setup_token_used_at=True,
        )

    async def consume_telegram_setup_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            UPDATE onboarding_sessions
            SET telegram_setup_token_used_at = now(), updated_at = now()
            WHERE session_id = $1
              AND telegram_setup_token_hash = $2
              AND telegram_setup_token_used_at IS NULL
              AND telegram_setup_token_expires_at > now()
            RETURNING session_id
            """,
            session_id,
            token_hash,
        )
        return row is not None

    async def save_telegram_setup(
        self,
        session_id: UUID,
        telegram: OnboardingTelegramSetup,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        payload = session_payload(session)
        payload["telegram"] = telegram.model_dump(mode="json")
        return await self._update_session_payload(
            session_id,
            status="ready_to_submit",
            current_step="submit",
            payload=payload,
        )

    async def save_provider_projects(
        self,
        session_id: UUID,
        provider_projects: OnboardingProviderProjects,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        payload = session_payload(session)
        payload["provider_projects"] = provider_projects.model_dump(mode="json")
        return await self._update_session_payload(
            session_id,
            status=session.status,
            current_step=session.current_step,
            payload=payload,
        )

    async def mark_session_submitted(
        self,
        session_id: UUID,
        *,
        job_id: UUID,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        return await self._update_session_payload(
            session_id,
            status="submitted",
            current_step="complete",
            payload=session_payload(session),
            submitted_job_id=job_id,
            error=None,
        )

    async def mark_session_failed(
        self,
        session_id: UUID,
        *,
        error: str,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        return await self._update_session_payload(
            session_id,
            status="failed",
            current_step=session.current_step,
            payload=session_payload(session),
            error=error,
        )

    async def _update_job(self, job_id: UUID, **updates) -> OnboardingJobRecord:
        assert self.database.pool
        existing = await self.get_job(job_id)
        if existing is None:
            raise KeyError(f"Onboarding job not found: {job_id}")
        row = await self.database.pool.fetchrow(
            """
            UPDATE onboarding_jobs
            SET status = $2,
                tenant_id = $3,
                tenant_slug = $4,
                error = $5,
                updated_at = now()
            WHERE job_id = $1
            RETURNING job_id, idempotency_key, status, tenant_id, tenant_slug, error,
                      created_at, updated_at
            """,
            job_id,
            updates.get("status", existing.status),
            updates.get("tenant_id", existing.tenant_id),
            updates.get("tenant_slug", existing.tenant_slug),
            updates.get("error", existing.error),
        )
        return row_to_onboarding_job(row)

    async def _require_session(self, session_id: UUID) -> OnboardingSessionRecord:
        session = await self.get_session(session_id)
        if session is None:
            raise KeyError(f"Onboarding session not found: {session_id}")
        return session

    async def _update_session_payload(
        self,
        session_id: UUID,
        *,
        status: str,
        current_step: str,
        payload: dict,
        website_url: str | None = None,
        website_verification_email: str | None = None,
        username_email_verified: bool | None = None,
        username_email_verification_token_hash: str | None = None,
        username_email_verification_expires_at: datetime | None = None,
        username_email_verification_token_used_at: datetime | None = None,
        clear_username_email_verification_token_used_at: bool = False,
        website_email_verified: bool | None = None,
        website_email_verification_token_hash: str | None = None,
        website_email_verification_expires_at: datetime | None = None,
        website_email_verification_token_used_at: datetime | None = None,
        clear_website_email_verification_token_used_at: bool = False,
        telegram_setup_url: str | None = None,
        telegram_setup_token_hash: str | None = None,
        telegram_setup_token_expires_at: datetime | None = None,
        telegram_setup_token_used_at: datetime | None = None,
        clear_telegram_setup_token_used_at: bool = False,
        submitted_job_id: UUID | None = None,
        error: str | None = None,
    ) -> OnboardingSessionRecord:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            UPDATE onboarding_sessions
            SET status = $2,
                current_step = $3,
                session_payload = $4::jsonb,
                website_url = COALESCE($5, website_url),
                website_verification_email = COALESCE($6, website_verification_email),
                username_email_verified = COALESCE($7, username_email_verified),
                username_email_verification_token_hash = COALESCE(
                    $8,
                    username_email_verification_token_hash
                ),
                username_email_verification_expires_at = COALESCE(
                    $9,
                    username_email_verification_expires_at
                ),
                username_email_verification_token_used_at = CASE
                    WHEN $10 THEN NULL
                    ELSE COALESCE($11, username_email_verification_token_used_at)
                END,
                website_email_verified = COALESCE($12, website_email_verified),
                website_email_verification_token_hash = COALESCE(
                    $13,
                    website_email_verification_token_hash
                ),
                website_email_verification_expires_at = COALESCE(
                    $14,
                    website_email_verification_expires_at
                ),
                website_email_verification_token_used_at = CASE
                    WHEN $15 THEN NULL
                    ELSE COALESCE($16, website_email_verification_token_used_at)
                END,
                telegram_setup_url = COALESCE($17, telegram_setup_url),
                telegram_setup_token_hash = COALESCE($18, telegram_setup_token_hash),
                telegram_setup_token_expires_at = COALESCE($19, telegram_setup_token_expires_at),
                telegram_setup_token_used_at = CASE
                    WHEN $23 THEN NULL
                    ELSE COALESCE($20, telegram_setup_token_used_at)
                END,
                submitted_job_id = COALESCE($21, submitted_job_id),
                error = $22,
                updated_at = now()
            WHERE session_id = $1
            RETURNING session_id, status, current_step, website_url, admin_email,
                      website_verification_email, session_payload,
                      username_email_verified, username_email_verification_expires_at,
                      website_email_verified, website_email_verification_expires_at,
                      telegram_setup_url,
                      telegram_setup_token_expires_at, submitted_job_id, error,
                      created_at, updated_at
            """,
            session_id,
            status,
            current_step,
            json.dumps(payload),
            website_url,
            website_verification_email,
            username_email_verified,
            username_email_verification_token_hash,
            username_email_verification_expires_at,
            clear_username_email_verification_token_used_at,
            username_email_verification_token_used_at,
            website_email_verified,
            website_email_verification_token_hash,
            website_email_verification_expires_at,
            clear_website_email_verification_token_used_at,
            website_email_verification_token_used_at,
            telegram_setup_url,
            telegram_setup_token_hash,
            telegram_setup_token_expires_at,
            telegram_setup_token_used_at,
            submitted_job_id,
            error,
            clear_telegram_setup_token_used_at,
        )
        return row_to_onboarding_session(row)


def schema(embedding_dimensions: int) -> str:
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL DEFAULT 'default',
    channel text NOT NULL,
    external_chat_id text NOT NULL,
    external_user_id text NOT NULL,
    state text NOT NULL DEFAULT 'BOT_ACTIVE'
        CHECK (state IN ('BOT_ACTIVE', 'HUMAN_REQUESTED', 'HUMAN_ACTIVE')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, channel, external_chat_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL DEFAULT 'default',
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    event_id text NOT NULL,
    sender_type text NOT NULL,
    body text NOT NULL,
    in_scope boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, event_id)
);

CREATE TABLE IF NOT EXISTS conversation_state_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    previous_state text NOT NULL,
    new_state text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id text PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    display_name text NOT NULL,
    selected_plan text NOT NULL DEFAULT 'sme'
        CHECK (selected_plan IN ('sme', 'enterprise')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_configs (
    tenant_id text PRIMARY KEY,
    selected_plan text NOT NULL DEFAULT 'sme'
        CHECK (selected_plan IN ('sme', 'enterprise')),
    answer_prompt_instructions text,
    planner_prompt_instructions text,
    llm_project_id text,
    llm_project_name text,
    langsmith_project text,
    llm_provider text,
    llm_model text,
    llm_base_url text,
    vector_provider text NOT NULL DEFAULT 'pgvector',
    vector_isolation_mode text NOT NULL DEFAULT 'shared_collection'
        CHECK (vector_isolation_mode IN ('shared_collection', 'dedicated_collection')),
    vector_collection text NOT NULL DEFAULT 'customer-service',
    vector_namespace text,
    telegram_secret_name text,
    whatsapp_secret_name text,
    web_search_provider text,
    web_search_project_name text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_enabled_features (
    tenant_id text NOT NULL REFERENCES tenant_configs(tenant_id) ON DELETE CASCADE,
    feature_key text NOT NULL,
    enabled_at timestamptz NOT NULL DEFAULT now(),
    enabled_by text,
    source text,
    PRIMARY KEY (tenant_id, feature_key)
);

CREATE TABLE IF NOT EXISTS onboarding_jobs (
    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'accepted'
        CHECK (status IN ('accepted', 'running', 'succeeded', 'failed')),
    tenant_id text,
    tenant_slug text,
    request_payload jsonb NOT NULL DEFAULT '{{}}',
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS onboarding_sessions (
    session_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'username_email_verification_pending',
            'website_verification_pending',
            'draft',
            'ready_for_review',
            'awaiting_telegram_setup',
            'ready_to_submit',
            'submitted',
            'failed'
    )),
    current_step text NOT NULL DEFAULT 'start',
    website_url text,
    admin_email text NOT NULL,
    session_payload jsonb NOT NULL DEFAULT '{{}}',
    website_verification_email text,
    username_email_verified boolean NOT NULL DEFAULT false,
    username_email_verification_token_hash text,
    username_email_verification_expires_at timestamptz,
    username_email_verification_token_used_at timestamptz,
    website_email_verified boolean NOT NULL DEFAULT false,
    website_email_verification_token_hash text,
    website_email_verification_expires_at timestamptz,
    website_email_verification_token_used_at timestamptz,
    telegram_setup_url text,
    telegram_setup_token_hash text,
    telegram_setup_token_expires_at timestamptz,
    telegram_setup_token_used_at timestamptz,
    submitted_job_id uuid REFERENCES onboarding_jobs(job_id),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tenant_memberships (
    tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    user_email text NOT NULL,
    user_name text NOT NULL,
    role text NOT NULL DEFAULT 'owner'
        CHECK (role IN ('owner', 'admin', 'agent')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_email)
);

CREATE TABLE IF NOT EXISTS business_profiles (
    tenant_id text PRIMARY KEY REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    business_name text NOT NULL,
    website_url text NOT NULL,
    location_name text NOT NULL,
    physical_location text NOT NULL,
    business_phone text NOT NULL,
    business_email text NOT NULL,
    google_place_url text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS business_contact_points (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id text NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    kind text NOT NULL,
    label text,
    value text,
    url text,
    is_primary boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace text NOT NULL,
    chunk_id text NOT NULL,
    source text NOT NULL,
    source_url text,
    content text NOT NULL,
    content_hash text,
    metadata jsonb NOT NULL DEFAULT '{{}}',
    embedding vector({embedding_dimensions}) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(namespace, chunk_id)
);

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default';

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'BOT_ACTIVE';

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE messages
ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default';

ALTER TABLE messages
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE messages
ADD COLUMN IF NOT EXISTS in_scope boolean NOT NULL DEFAULT true;

ALTER TABLE tenants
ADD COLUMN IF NOT EXISTS slug text;

ALTER TABLE tenants
ADD COLUMN IF NOT EXISTS display_name text;

ALTER TABLE tenants
ADD COLUMN IF NOT EXISTS selected_plan text NOT NULL DEFAULT 'sme';

ALTER TABLE tenants
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE tenants
ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE conversations
DROP CONSTRAINT IF EXISTS conversations_channel_external_chat_id_key;

ALTER TABLE messages
DROP CONSTRAINT IF EXISTS messages_event_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS conversations_tenant_channel_chat_idx
ON conversations(tenant_id, channel, external_chat_id);

CREATE UNIQUE INDEX IF NOT EXISTS messages_tenant_event_id_idx
ON messages(tenant_id, event_id);

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS selected_plan text NOT NULL DEFAULT 'sme';

INSERT INTO tenants(tenant_id, slug, display_name, selected_plan)
SELECT
    tenant_id,
    regexp_replace(lower(tenant_id), '[^a-z0-9]+', '-', 'g') || '-' || substr(md5(tenant_id), 1, 8),
    tenant_id,
    selected_plan
FROM tenant_configs
ON CONFLICT (tenant_id) DO NOTHING;

UPDATE tenants
SET slug = regexp_replace(lower(tenant_id), '[^a-z0-9]+', '-', 'g')
    || '-'
    || substr(md5(tenant_id), 1, 8)
WHERE slug IS NULL OR btrim(slug) = '';

UPDATE tenants
SET display_name = tenant_id
WHERE display_name IS NULL OR btrim(display_name) = '';

ALTER TABLE tenants
ALTER COLUMN slug SET NOT NULL;

ALTER TABLE tenants
ALTER COLUMN display_name SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS tenants_slug_idx
ON tenants(slug);

ALTER TABLE tenant_configs
DROP COLUMN IF EXISTS enabled_features;

ALTER TABLE tenant_configs
DROP COLUMN IF EXISTS category;

CREATE INDEX IF NOT EXISTS tenant_enabled_features_feature_key_idx
ON tenant_enabled_features(feature_key);

CREATE INDEX IF NOT EXISTS onboarding_jobs_status_idx
ON onboarding_jobs(status);

CREATE INDEX IF NOT EXISTS onboarding_sessions_status_idx
ON onboarding_sessions(status);

CREATE INDEX IF NOT EXISTS onboarding_sessions_admin_email_idx
ON onboarding_sessions(admin_email);

ALTER TABLE onboarding_sessions
ALTER COLUMN website_url DROP NOT NULL;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS website_verification_email text;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS username_email_verified boolean NOT NULL DEFAULT false;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS username_email_verification_token_hash text;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS username_email_verification_expires_at timestamptz;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS username_email_verification_token_used_at timestamptz;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS website_email_verified boolean NOT NULL DEFAULT false;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS website_email_verification_token_hash text;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS website_email_verification_expires_at timestamptz;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS website_email_verification_token_used_at timestamptz;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS admin_email_verified boolean NOT NULL DEFAULT false;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS admin_email_verification_token_hash text;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS admin_email_verification_expires_at timestamptz;

ALTER TABLE onboarding_sessions
ADD COLUMN IF NOT EXISTS admin_email_verification_token_used_at timestamptz;

UPDATE onboarding_sessions
SET username_email_verified = admin_email_verified,
    username_email_verification_token_hash = COALESCE(
        username_email_verification_token_hash,
        admin_email_verification_token_hash
    ),
    username_email_verification_expires_at = COALESCE(
        username_email_verification_expires_at,
        admin_email_verification_expires_at
    ),
    username_email_verification_token_used_at = COALESCE(
        username_email_verification_token_used_at,
        admin_email_verification_token_used_at
    )
WHERE EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = 'onboarding_sessions'
      AND column_name = 'admin_email_verified'
);

UPDATE onboarding_sessions
SET website_verification_email = COALESCE(website_verification_email, admin_email),
    website_email_verified = COALESCE(website_email_verified, username_email_verified)
WHERE website_url IS NOT NULL
  AND website_verification_email IS NULL;

UPDATE onboarding_sessions
SET status = 'username_email_verification_pending'
WHERE status = 'email_verification_pending';

ALTER TABLE onboarding_sessions
DROP CONSTRAINT IF EXISTS onboarding_sessions_status_check;

ALTER TABLE onboarding_sessions
ADD CONSTRAINT onboarding_sessions_status_check
CHECK (status IN (
    'username_email_verification_pending',
    'website_verification_pending',
    'draft',
    'ready_for_review',
    'awaiting_telegram_setup',
    'ready_to_submit',
    'submitted',
    'failed'
));

CREATE INDEX IF NOT EXISTS business_contact_points_tenant_id_idx
ON business_contact_points(tenant_id);

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS llm_project_id text;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS llm_project_name text;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenant_configs' AND column_name = 'openai_project_id'
    ) THEN
        UPDATE tenant_configs
        SET llm_project_id = openai_project_id
        WHERE openai_project_id IS NOT NULL;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenant_configs' AND column_name = 'openai_project_name'
    ) THEN
        UPDATE tenant_configs
        SET llm_project_name = openai_project_name
        WHERE openai_project_name IS NOT NULL;
    END IF;
END $$;

ALTER TABLE tenant_configs
DROP COLUMN IF EXISTS openai_project_id;

ALTER TABLE tenant_configs
DROP COLUMN IF EXISTS openai_project_name;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS langsmith_project text;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS llm_provider text;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS llm_model text;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS llm_base_url text;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS vector_provider text NOT NULL DEFAULT 'pgvector';

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS vector_isolation_mode text NOT NULL DEFAULT 'shared_collection';

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS vector_collection text NOT NULL DEFAULT 'customer-service';

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS vector_namespace text;

UPDATE tenant_configs
SET vector_namespace = CASE
    WHEN vector_namespace = 'seed-knowledge' THEN 'default'
    ELSE regexp_replace(vector_namespace, ':seed-knowledge$', '')
END
WHERE vector_namespace = 'seed-knowledge'
   OR vector_namespace LIKE '%:seed-knowledge';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenant_configs' AND column_name = 'pinecone_index'
    ) THEN
        UPDATE tenant_configs
        SET vector_collection = pinecone_index
        WHERE pinecone_index IS NOT NULL;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenant_configs' AND column_name = 'pinecone_namespace'
    ) THEN
        UPDATE tenant_configs
        SET vector_namespace = pinecone_namespace
        WHERE pinecone_namespace IS NOT NULL;
    END IF;
END $$;

ALTER TABLE tenant_configs
DROP COLUMN IF EXISTS pinecone_index;

ALTER TABLE tenant_configs
DROP COLUMN IF EXISTS pinecone_namespace;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS telegram_secret_name text;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS whatsapp_secret_name text;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS web_search_provider text;

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS web_search_project_name text;

ALTER TABLE tenant_configs
DROP COLUMN IF EXISTS web_search_api_key_id;

ALTER TABLE tenant_configs
DROP COLUMN IF EXISTS web_search_secret_name;

ALTER TABLE knowledge_documents
ADD COLUMN IF NOT EXISTS chunk_id text;

ALTER TABLE knowledge_documents
ADD COLUMN IF NOT EXISTS source_url text;

UPDATE knowledge_documents
SET chunk_id = source
WHERE chunk_id IS NULL;

ALTER TABLE knowledge_documents
ALTER COLUMN chunk_id SET NOT NULL;

ALTER TABLE knowledge_documents
ADD COLUMN IF NOT EXISTS content_hash text;

ALTER TABLE knowledge_documents
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE knowledge_documents
ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

DELETE FROM knowledge_documents old
USING knowledge_documents existing
WHERE (
    (old.namespace = 'seed-knowledge' AND existing.namespace = 'default')
    OR (
        old.namespace LIKE '%:seed-knowledge'
        AND existing.namespace = regexp_replace(old.namespace, ':seed-knowledge$', '')
    )
)
  AND old.chunk_id = existing.chunk_id
  AND old.ctid <> existing.ctid;

UPDATE knowledge_documents
SET namespace = CASE
    WHEN namespace = 'seed-knowledge' THEN 'default'
    ELSE regexp_replace(namespace, ':seed-knowledge$', '')
END
WHERE namespace = 'seed-knowledge'
   OR namespace LIKE '%:seed-knowledge';

ALTER TABLE knowledge_documents
DROP CONSTRAINT IF EXISTS knowledge_documents_namespace_source_key;

DROP INDEX IF EXISTS knowledge_documents_namespace_source_idx;

DELETE FROM knowledge_documents older
USING knowledge_documents newer
WHERE older.namespace = newer.namespace
  AND older.chunk_id = newer.chunk_id
  AND older.ctid < newer.ctid;

CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_namespace_chunk_id_idx
ON knowledge_documents(namespace, chunk_id);

CREATE INDEX IF NOT EXISTS knowledge_documents_namespace_source_url_idx
ON knowledge_documents(namespace, source_url);
"""
