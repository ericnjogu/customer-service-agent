import logging
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.tenancy import SEED_KNOWLEDGE_NAMESPACE, tenant_knowledge_namespace

__all__ = [
    "SEED_KNOWLEDGE_NAMESPACE",
    "tenant_knowledge_namespace",
    "load_knowledge_documents",
    "chunk_text",
]

logger = logging.getLogger(__name__)

SUPPORTED_KNOWLEDGE_EXTENSIONS = {".md", ".txt"}
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


def load_knowledge_documents(
    knowledge_path: str | None,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Document]:
    if not knowledge_path:
        logger.info("SUPPORT_KNOWLEDGE_PATH is unset; no seed knowledge documents will be loaded")
        return []

    root = Path(knowledge_path)
    logger.info("Loading seed knowledge documents from directory: %s", root)
    if not root.exists():
        raise FileNotFoundError(f"Knowledge path does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Knowledge path must be a directory: {root}")

    documents: list[Document] = []
    skipped_paths: list[str] = []
    for path in sorted(root.iterdir()):
        if path.name.startswith(".."):
            logger.debug("Skipping Kubernetes projected-volume metadata path=%s", path.name)
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_KNOWLEDGE_EXTENSIONS:
            skipped_paths.append(path.relative_to(root).as_posix())
            continue

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            skipped_paths.append(path.relative_to(root).as_posix())
            continue

        relative_path = path.relative_to(root).as_posix()
        logger.info(
            "Loaded seed knowledge file path=%s bytes=%d",
            relative_path,
            len(content.encode("utf-8")),
        )
        chunks = chunk_text(content, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        for chunk_index, chunk in enumerate(chunks):
            source = f"kb/{relative_path}"
            documents.append(
                Document(
                    page_content=chunk,
                    metadata={
                        "source": source,
                        "chunk_id": f"{source}#{chunk_index:04d}",
                        "chunk_index": chunk_index,
                        "chunk_count": len(chunks),
                        "filename": path.name,
                    },
                )
            )

    if skipped_paths:
        logger.info(
            "Skipped %d unsupported or empty knowledge file(s): %s",
            len(skipped_paths),
            skipped_paths,
        )
    if not documents:
        logger.warning(
            "No supported seed knowledge documents found in %s; supported extensions are %s",
            root,
            sorted(SUPPORTED_KNOWLEDGE_EXTENSIONS),
        )

    return documents
