from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.tenancy import (
    SEED_KNOWLEDGE_NAMESPACE,
    tenant_knowledge_namespace,
)

__all__ = [
    "SEED_KNOWLEDGE_NAMESPACE",
    "tenant_knowledge_namespace",
    "chunk_text",
]

DEFAULT_CHUNK_SIZE = 1_200
DEFAULT_CHUNK_OVERLAP = 200


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must not be negative")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    text = text.strip()
    if not text:
        return []
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    return [chunk.strip() for chunk in splitter.split_text(text) if chunk.strip()]
