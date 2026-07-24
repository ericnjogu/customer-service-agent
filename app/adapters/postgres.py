import json
import logging
from datetime import datetime
from uuid import UUID

import asyncpg
from langchain_core.documents import Document

from app.models import (
    ConversationRecord,
    IncomingMessage,
    StoredMessage,
)
from app.ports import EmbeddingProvider

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
            INSERT INTO conversations(channel, external_chat_id, external_user_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (channel, external_chat_id) DO UPDATE
            SET external_user_id = EXCLUDED.external_user_id, updated_at = now()
            RETURNING id, channel, external_chat_id, external_user_id, state, created_at
            """,
            message.channel,
            message.external_chat_id,
            message.external_user_id,
        )
        return ConversationRecord(**dict(row))

    async def get_by_id(self, conversation_id: UUID) -> ConversationRecord | None:
        assert self.database.pool
        row = await self.database.pool.fetchrow(
            """
            SELECT id, channel, external_chat_id, external_user_id, state, created_at
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
                    RETURNING id, channel, external_chat_id, external_user_id, state, created_at
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
            INSERT INTO messages(conversation_id, event_id, sender_type, body, created_at)
            VALUES ($1, $2, $3, $4, $5) ON CONFLICT (event_id) DO NOTHING
            """,
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
            SELECT conversation_id, event_id, sender_type, body, created_at
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


def schema(embedding_dimensions: int) -> str:
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel text NOT NULL,
    external_chat_id text NOT NULL,
    external_user_id text NOT NULL,
    state text NOT NULL DEFAULT 'BOT_ACTIVE'
        CHECK (state IN ('BOT_ACTIVE', 'HUMAN_REQUESTED', 'HUMAN_ACTIVE')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(channel, external_chat_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    event_id text NOT NULL UNIQUE,
    sender_type text NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversation_state_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    previous_state text NOT NULL,
    new_state text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
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
ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'BOT_ACTIVE';

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE messages
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

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
