import json
import logging
from datetime import datetime
from uuid import UUID

import asyncpg
from langchain_core.documents import Document

from app.models import (
    ConversationRecord,
    IncomingMessage,
    KnowledgeIngestionJob,
    KnowledgeIngestionResult,
    StoredMessage,
    TenantConfig,
    TenantPlan,
    TenantRecord,
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


def decode_metadata(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    return {}


def row_to_tenant_config(row: asyncpg.Record) -> TenantConfig:
    data = dict(row)
    data["enabled_features"] = [
        str(item) for item in (data.get("enabled_features") or [])
    ]
    return TenantConfig(**data)


def row_to_tenant(row: asyncpg.Record) -> TenantRecord:
    return TenantRecord(**dict(row))


def row_to_knowledge_ingestion_job(row: asyncpg.Record) -> KnowledgeIngestionJob:
    data = dict(row)
    chunk_ids = data.get("chunk_ids")
    if isinstance(chunk_ids, str):
        chunk_ids = json.loads(chunk_ids)
    data["chunk_ids"] = chunk_ids or []
    return KnowledgeIngestionJob(**data)


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
                "the KB after changing SUPPORT_EMBEDDING_DIMENSIONS."
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
                created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (tenant_id, event_id) DO NOTHING
            """,
            message.tenant_id,
            message.conversation_id,
            message.event_id,
            message.sender_type,
            message.body,
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
            SELECT tenant_id, conversation_id, event_id, sender_type, body, created_at
            FROM messages
            WHERE conversation_id = $1 AND created_at >= $2
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
                await connection.execute(
                    """
                    INSERT INTO knowledge_documents(
                        namespace,
                        chunk_id,
                        source,
                        content,
                        content_hash,
                        metadata,
                        embedding
                    )
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::vector)
                    ON CONFLICT (namespace, chunk_id) DO UPDATE
                    SET content = EXCLUDED.content,
                        source = EXCLUDED.source,
                        content_hash = EXCLUDED.content_hash,
                        metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding,
                        updated_at = now()
                    """,
                    namespace,
                    chunk_id,
                    source,
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
            SELECT content, metadata, created_at, 1 - (embedding <=> $1::vector) AS score
            FROM knowledge_documents WHERE namespace = $2
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
        default_vector_collection: str = "customer-support",
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
                whatsapp_secret_name
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
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


class PostgresKnowledgeIngestionJobRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    async def initialize(self) -> None:
        return None

    async def create(
        self,
        *,
        job_id: str,
        tenant_id: str,
        filename: str,
        content_type: str,
        object_bucket: str,
        object_key: str,
        object_etag: str | None = None,
    ) -> KnowledgeIngestionJob:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            INSERT INTO knowledge_ingestion_jobs(
                job_id,
                tenant_id,
                status,
                filename,
                content_type,
                object_bucket,
                object_key,
                object_etag
            )
            VALUES ($1, $2, 'PENDING', $3, $4, $5, $6, $7)
            RETURNING *
            """,
            job_id,
            normalize_tenant_id(tenant_id),
            filename,
            content_type,
            object_bucket,
            object_key,
            object_etag,
        )
        job = row_to_knowledge_ingestion_job(row)
        logger.info(
            "Created Postgres knowledge ingestion job job_id=%s tenant_id=%s status=%s "
            "bucket=%s key=%s",
            job.job_id,
            job.tenant_id,
            job.status,
            job.object_bucket,
            job.object_key,
        )
        return job

    async def get(self, tenant_id: str, job_id: str) -> KnowledgeIngestionJob | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT *
            FROM knowledge_ingestion_jobs
            WHERE tenant_id = $1 AND job_id = $2
            """,
            normalize_tenant_id(tenant_id),
            job_id,
        )
        return row_to_knowledge_ingestion_job(row) if row else None

    async def get_by_id(self, job_id: str) -> KnowledgeIngestionJob | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT *
            FROM knowledge_ingestion_jobs
            WHERE job_id = $1
            """,
            job_id,
        )
        return row_to_knowledge_ingestion_job(row) if row else None

    async def mark_running(self, job_id: str) -> KnowledgeIngestionJob:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            UPDATE knowledge_ingestion_jobs
            SET status = 'RUNNING',
                started_at = COALESCE(started_at, now()),
                error_message = NULL,
                updated_at = now()
            WHERE job_id = $1
            RETURNING *
            """,
            job_id,
        )
        job = row_to_knowledge_ingestion_job(row)
        logger.info(
            "Marked Postgres knowledge ingestion job running job_id=%s tenant_id=%s",
            job.job_id,
            job.tenant_id,
        )
        return job

    async def mark_succeeded(
        self,
        job_id: str,
        *,
        result: KnowledgeIngestionResult,
    ) -> KnowledgeIngestionJob:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            UPDATE knowledge_ingestion_jobs
            SET status = 'SUCCEEDED',
                pages_read = $2,
                pages_with_text = $3,
                chunks_created = $4,
                chunk_ids = $5::jsonb,
                error_message = NULL,
                finished_at = now(),
                updated_at = now()
            WHERE job_id = $1
            RETURNING *
            """,
            job_id,
            result.pages_read,
            result.pages_with_text,
            result.chunks_created,
            json.dumps(result.chunk_ids),
        )
        job = row_to_knowledge_ingestion_job(row)
        logger.info(
            "Marked Postgres knowledge ingestion job succeeded job_id=%s tenant_id=%s "
            "chunks_created=%d",
            job.job_id,
            job.tenant_id,
            job.chunks_created,
        )
        return job

    async def mark_failed(self, job_id: str, *, error_message: str) -> KnowledgeIngestionJob:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            UPDATE knowledge_ingestion_jobs
            SET status = 'FAILED',
                error_message = $2,
                finished_at = now(),
                updated_at = now()
            WHERE job_id = $1
            RETURNING *
            """,
            job_id,
            error_message[:2_000],
        )
        job = row_to_knowledge_ingestion_job(row)
        logger.info(
            "Marked Postgres knowledge ingestion job failed job_id=%s tenant_id=%s error=%s",
            job.job_id,
            job.tenant_id,
            job.error_message,
        )
        return job


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
    vector_collection text NOT NULL DEFAULT 'customer-support',
    vector_namespace text,
    telegram_secret_name text,
    whatsapp_secret_name text,
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

CREATE TABLE IF NOT EXISTS knowledge_ingestion_jobs (
    job_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    status text NOT NULL
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    filename text NOT NULL,
    content_type text NOT NULL,
    object_bucket text NOT NULL,
    object_key text NOT NULL,
    object_etag text,
    pages_read integer NOT NULL DEFAULT 0,
    pages_with_text integer NOT NULL DEFAULT 0,
    chunks_created integer NOT NULL DEFAULT 0,
    chunk_ids jsonb NOT NULL DEFAULT '[]',
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace text NOT NULL,
    chunk_id text NOT NULL,
    source text NOT NULL,
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

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS object_etag text;

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS pages_read integer NOT NULL DEFAULT 0;

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS pages_with_text integer NOT NULL DEFAULT 0;

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS chunks_created integer NOT NULL DEFAULT 0;

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS chunk_ids jsonb NOT NULL DEFAULT '[]';

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS error_message text;

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS started_at timestamptz;

ALTER TABLE knowledge_ingestion_jobs
ADD COLUMN IF NOT EXISTS finished_at timestamptz;

CREATE INDEX IF NOT EXISTS knowledge_ingestion_jobs_tenant_status_idx
ON knowledge_ingestion_jobs(tenant_id, status);

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
ADD COLUMN IF NOT EXISTS vector_collection text NOT NULL DEFAULT 'customer-support';

ALTER TABLE tenant_configs
ADD COLUMN IF NOT EXISTS vector_namespace text;

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

ALTER TABLE knowledge_documents
ADD COLUMN IF NOT EXISTS chunk_id text;

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
"""
