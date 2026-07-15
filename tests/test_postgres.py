import pytest

from app.adapters.embeddings import LocalHashEmbeddingProvider
from app.adapters.postgres import decode_metadata, schema


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
