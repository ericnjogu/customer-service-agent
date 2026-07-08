import hashlib
import json
import logging
import math
import re

import asyncpg
from langchain_core.documents import Document

from app.models import ConversationRecord, IncomingMessage, StoredMessage

EMBEDDING_DIMENSIONS = 64
logger = logging.getLogger(__name__)


def local_embedding(text: str) -> list[float]:
    """Deterministic, dependency-free embedding for the local MVP only."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % EMBEDDING_DIMENSIONS
        vector[index] += 1.0 if digest[2] % 2 else -1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in values) + "]"


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
            RETURNING id, channel, external_chat_id, external_user_id, status
            """,
            message.channel,
            message.external_chat_id,
            message.external_user_id,
        )
        return ConversationRecord(**dict(row))

    async def save_message(self, message: StoredMessage) -> bool:
        assert self.database.pool
        result = await self.database.pool.execute(
            """
            INSERT INTO messages(conversation_id, event_id, sender_type, body)
            VALUES ($1, $2, $3, $4) ON CONFLICT (event_id) DO NOTHING
            """,
            message.conversation_id,
            message.event_id,
            message.sender_type,
            message.body,
        )
        return result == "INSERT 0 1"


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
        rows = await self.database.pool.fetch(
            """
            SELECT content, metadata, 1 - (embedding <=> $1::vector) AS score
            FROM knowledge_documents WHERE namespace = $2
            ORDER BY embedding <=> $1::vector LIMIT $3
            """,
            vector_literal(local_embedding(query)),
            namespace,
            limit,
        )
        return [
            Document(
                page_content=row["content"],
                metadata={**row["metadata"], "score": row["score"]},
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
