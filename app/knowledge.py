import hashlib
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.tenancy import (
    SEED_KNOWLEDGE_NAMESPACE,
    tenant_knowledge_namespace,
)

__all__ = [
    "SEED_KNOWLEDGE_NAMESPACE",
    "tenant_knowledge_namespace",
    "KnowledgeChunk",
    "chunk_text",
    "chunk_text_with_metadata",
    "stable_source_hash",
]

DEFAULT_CHUNK_SIZE = 1_000
DEFAULT_CHUNK_OVERLAP = 180


@dataclass(frozen=True)
class KnowledgeChunk:
    content: str
    section_title: str | None
    chunk_index: int
    chunk_count: int
    content_hash: str


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    return [
        chunk.content
        for chunk in chunk_text_with_metadata(
            text,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    ]


def chunk_text_with_metadata(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[KnowledgeChunk]:
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
    draft_chunks = [
        chunk.strip()
        for chunk in splitter.split_text(text)
        if chunk.strip()
    ]

    chunk_count = len(draft_chunks)
    return [
        KnowledgeChunk(
            content=content,
            section_title=None,
            chunk_index=index,
            chunk_count=chunk_count,
            content_hash=stable_source_hash(content, length=64),
        )
        for index, content in enumerate(draft_chunks)
    ]


def stable_source_hash(value: str, *, length: int = 16) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()[:length]
