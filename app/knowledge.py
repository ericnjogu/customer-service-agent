import logging
from pathlib import Path

from langchain_core.documents import Document

SEED_KNOWLEDGE_NAMESPACE = "seed-knowledge"
logger = logging.getLogger(__name__)

DEMO_DOCUMENTS = [
    Document(
        page_content=(
            "To reset your password, open Settings, select Security, then choose Reset password. "
            "A reset link will be sent to your verified email address."
        ),
        metadata={"source": "kb/password-reset"},
    ),
    Document(
        page_content=(
            "Refund requests can be submitted within 30 days of purchase. Include the order "
            "number and the reason for the request."
        ),
        metadata={"source": "kb/refunds"},
    ),
]

SUPPORTED_KNOWLEDGE_EXTENSIONS = {".md", ".txt"}


def load_knowledge_documents(knowledge_path: str | None) -> list[Document]:
    if not knowledge_path:
        logger.warning(
            "SUPPORT_KNOWLEDGE_PATH is unset; using %d demo seed knowledge document(s)",
            len(DEMO_DOCUMENTS),
        )
        return DEMO_DOCUMENTS

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
        documents.append(
            Document(
                page_content=content,
                metadata={
                    "source": f"kb/{relative_path}",
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
