import inspect

import pytest

from app.adapters.embeddings import LocalHashEmbeddingProvider
from app.adapters.postgres import PgVectorRetrievalStore, decode_metadata, schema


def dot(left: list[float], right: list[float]) -> float:
    return sum(
        left_value * right_value
        for left_value, right_value in zip(left, right, strict=True)
    )


async def test_local_embedding_ignores_stop_words() -> None:
    embeddings = LocalHashEmbeddingProvider(dimensions=64)

    assert await embeddings.embed_query("how do i") == pytest.approx(
        await embeddings.embed_query("")
    )


async def test_local_embedding_aligns_query_terms_with_document_terms() -> None:
    embeddings = LocalHashEmbeddingProvider(dimensions=64)
    query = await embeddings.embed_query("How do I reset my password?")
    relevant_document = await embeddings.embed_query("Reset password from Settings and Security.")
    unrelated_document = await embeddings.embed_query("Refund requests require an order number.")

    assert dot(query, relevant_document) > dot(query, unrelated_document)


def test_decode_metadata_accepts_dict_or_json_string() -> None:
    assert decode_metadata({"source": "kb/refunds.txt"}) == {"source": "kb/refunds.txt"}
    assert decode_metadata('{"source": "kb/refunds.txt"}') == {"source": "kb/refunds.txt"}


def test_pgvector_schema_uses_chunk_id_uniqueness() -> None:
    ddl = schema(1536)

    assert "chunk_id text NOT NULL" in ddl
    assert "UNIQUE(namespace, chunk_id)" in ddl
    assert "UNIQUE(namespace, source)" not in ddl
    assert "knowledge_documents_namespace_chunk_id_idx" in ddl


def test_postgres_schema_scopes_conversations_and_events_by_tenant() -> None:
    ddl = schema(1536)

    assert "tenant_id text NOT NULL DEFAULT 'default'" in ddl
    assert "UNIQUE(tenant_id, channel, external_chat_id)" in ddl
    assert "UNIQUE(tenant_id, event_id)" in ddl
    assert "conversations_tenant_channel_chat_idx" in ddl
    assert "messages_tenant_event_id_idx" in ddl


def test_postgres_schema_includes_tenant_prompt_config() -> None:
    ddl = schema(1536)

    assert "CREATE TABLE IF NOT EXISTS tenants" in ddl
    assert "tenant_id text PRIMARY KEY" in ddl
    assert "slug text NOT NULL UNIQUE" in ddl
    assert "display_name text NOT NULL" in ddl
    assert "INSERT INTO tenants(tenant_id, slug, display_name, selected_plan)" in ddl
    assert "CREATE UNIQUE INDEX IF NOT EXISTS tenants_slug_idx" in ddl
    assert "CREATE TABLE IF NOT EXISTS tenant_configs" in ddl
    assert "tenant_id text PRIMARY KEY" in ddl
    assert "selected_plan text NOT NULL DEFAULT 'sme'" in ddl
    assert "CHECK (selected_plan IN ('sme', 'enterprise'))" in ddl
    assert "CREATE TABLE IF NOT EXISTS tenant_enabled_features" in ddl
    assert "tenant_id text NOT NULL REFERENCES tenant_configs(tenant_id)" in ddl
    assert "feature_key text NOT NULL" in ddl
    assert "PRIMARY KEY (tenant_id, feature_key)" in ddl
    assert "DROP COLUMN IF EXISTS enabled_features" in ddl
    assert "DROP COLUMN IF EXISTS category" in ddl
    assert "answer_prompt_instructions text" in ddl
    assert "planner_prompt_instructions text" in ddl
    assert "llm_project_id text" in ddl
    assert "llm_project_name text" in ddl
    assert "DROP COLUMN IF EXISTS openai_project_id" in ddl
    assert "DROP COLUMN IF EXISTS openai_project_name" in ddl
    assert "langsmith_project text" in ddl
    assert "llm_provider text" in ddl
    assert "llm_model text" in ddl
    assert "llm_base_url text" in ddl
    assert "vector_provider text NOT NULL DEFAULT 'pgvector'" in ddl
    assert "vector_isolation_mode text NOT NULL DEFAULT 'shared_collection'" in ddl
    assert "vector_collection text NOT NULL DEFAULT 'customer-service'" in ddl
    assert "vector_namespace text" in ddl
    assert "DROP COLUMN IF EXISTS pinecone_index" in ddl
    assert "DROP COLUMN IF EXISTS pinecone_namespace" in ddl
    assert "telegram_secret_name text" in ddl
    assert "whatsapp_secret_name text" in ddl


def test_pgvector_search_selects_chunk_creation_timestamp() -> None:
    source = inspect.getsource(PgVectorRetrievalStore.search)

    assert "SELECT content, metadata, created_at" in source
    assert '"created_at": row["created_at"].isoformat()' in source
