import hashlib
import re
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
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


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

    draft_chunks: list[tuple[str, str | None]] = []
    for section_title, section_text in markdown_sections(text):
        draft_chunks.extend(
            split_section(
                section_text,
                section_title=section_title,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        )

    chunk_count = len(draft_chunks)
    return [
        KnowledgeChunk(
            content=content,
            section_title=section_title,
            chunk_index=index,
            chunk_count=chunk_count,
            content_hash=stable_source_hash(content, length=64),
        )
        for index, (content, section_title) in enumerate(draft_chunks)
    ]


def markdown_sections(text: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        heading = HEADING_PATTERN.match(line)
        if heading and current_lines:
            sections.append((current_title, current_lines))
            current_title = heading.group(2).strip()
            current_lines = [line]
            continue
        if heading and not current_lines:
            current_title = heading.group(2).strip()
        current_lines.append(line)

    if current_lines:
        sections.append((current_title, current_lines))

    return [
        (section_title, "\n".join(lines).strip())
        for section_title, lines in sections
        if "\n".join(lines).strip()
    ]


def split_section(
    text: str,
    *,
    section_title: str | None,
    chunk_size: int,
    chunk_overlap: int,
) -> list[tuple[str, str | None]]:
    if len(text) <= chunk_size:
        return [(text, section_title)]

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    chunks = []
    for chunk in splitter.split_text(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        if section_title and not chunk.startswith("#"):
            chunk = f"# {section_title}\n\n{chunk}"
        chunks.append((chunk, section_title))
    return chunks


def stable_source_hash(value: str, *, length: int = 16) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()[:length]
