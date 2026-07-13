import hashlib
import json
import logging
import math
import re
from datetime import datetime
from uuid import UUID

import asyncpg
from langchain_core.documents import Document

from app.models import (
    ConversationRecord,
    IncomingMessage,
    StoredMessage,
)

EMBEDDING_DIMENSIONS = 64
logger = logging.getLogger(__name__)
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "do",
    "for",
    "how",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "the",
    "to",
    "what",
    "you",
    "your",
}


def embedding_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - STOP_WORDS


def local_embedding(text: str) -> list[float]:
    """Deterministic, dependency-free token embedding for the local MVP only."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token in embedding_tokens(text):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % EMBEDDING_DIMENSIONS
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


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
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url)
        async with self.pool.acquire() as connection:
            await connection.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()


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
            RETURNING id, channel, external_chat_id, external_user_id, status, issue_status,
                created_at
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
            SELECT id, channel, external_chat_id, external_user_id, status, issue_status,
                created_at
            FROM conversations WHERE id = $1
            """,
            conversation_id,
        )
        return ConversationRecord(**dict(row)) if row else None

    async def update_status(
        self,
        conversation_id: UUID,
        *,
        status: str | None = None,
        issue_status: str | None = None,
        reason: str | None = None,
    ) -> ConversationRecord:
        assert self.database.pool
        async with self.database.pool.acquire() as connection:
            async with connection.transaction():
                previous = await connection.fetchrow(
                    """
                    SELECT status, issue_status FROM conversations WHERE id = $1
                    FOR UPDATE
                    """,
                    conversation_id,
                )
                if not previous:
                    raise KeyError(f"Conversation not found: {conversation_id}")

                row = await connection.fetchrow(
                    """
                    UPDATE conversations
                    SET status = COALESCE($2, status),
                        issue_status = COALESCE($3, issue_status),
                        updated_at = now()
                    WHERE id = $1
                    RETURNING id, channel, external_chat_id, external_user_id, status,
                        issue_status, created_at
                    """,
                    conversation_id,
                    status,
                    issue_status,
                )
                await connection.execute(
                    """
                    INSERT INTO conversation_status_events(
                        conversation_id,
                        previous_status,
                        new_status,
                        previous_issue_status,
                        new_issue_status,
                        reason
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    conversation_id,
                    previous["status"],
                    row["status"],
                    previous["issue_status"],
                    row["issue_status"],
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


class PgVectorRetrievalStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

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
            for document in documents:
                source = str(document.metadata.get("source", "unknown"))
                logger.info(
                    "Upserting pgvector knowledge document namespace=%s source=%s content_chars=%d",
                    namespace,
                    source,
                    len(document.page_content),
                )
                await connection.execute(
                    """
                    INSERT INTO knowledge_documents(namespace, source, content, metadata, embedding)
                    VALUES ($1, $2, $3, $4::jsonb, $5::vector)
                    ON CONFLICT (namespace, source) DO UPDATE
                    SET content = EXCLUDED.content, metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding, updated_at = now()
                    """,
                    namespace,
                    source,
                    document.page_content,
                    json.dumps(document.metadata),
                    vector_literal(local_embedding(document.page_content)),
                )
        logger.info("Finished pgvector knowledge upsert for namespace=%s", namespace)

    async def search(self, query: str, namespace: str, limit: int = 4) -> list[Document]:
        assert self.database.pool
        query_embedding = local_embedding(query)
        if is_zero_vector(query_embedding):
            logger.debug(
                "Skipping pgvector search for namespace=%s because query has no embedding tokens",
                namespace,
            )
            return []

        rows = await self.database.pool.fetch(
            """
            SELECT content, metadata, 1 - (embedding <=> $1::vector) AS score
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
                metadata={**decode_metadata(row["metadata"]), "score": row["score"]},
            )
            for row in rows
            if row["score"] > 0
        ]


SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel text NOT NULL,
    external_chat_id text NOT NULL,
    external_user_id text NOT NULL,
    status text NOT NULL DEFAULT 'BOT_ACTIVE',
    issue_status text NOT NULL DEFAULT 'NEW',
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

CREATE TABLE IF NOT EXISTS conversation_status_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES conversations(id),
    previous_status text NOT NULL,
    new_status text NOT NULL,
    previous_issue_status text NOT NULL,
    new_issue_status text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace text NOT NULL,
    source text NOT NULL,
    content text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{{}}',
    embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(namespace, source)
);

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS issue_status text NOT NULL DEFAULT 'NEW';

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE messages
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE knowledge_documents
ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE knowledge_documents
ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

DELETE FROM knowledge_documents older
USING knowledge_documents newer
WHERE older.namespace = newer.namespace
  AND older.source = newer.source
  AND older.ctid < newer.ctid;

CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_namespace_source_idx
ON knowledge_documents(namespace, source);
"""
