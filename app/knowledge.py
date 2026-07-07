from pathlib import Path

from langchain_core.documents import Document

SEED_KNOWLEDGE_NAMESPACE = "seed-knowledge"

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
        return DEMO_DOCUMENTS

    root = Path(knowledge_path)
    if not root.exists():
        raise FileNotFoundError(f"Knowledge path does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Knowledge path must be a directory: {root}")

    documents: list[Document] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_KNOWLEDGE_EXTENSIONS:
            continue

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            continue

        relative_path = path.relative_to(root).as_posix()
        documents.append(
            Document(
                page_content=content,
                metadata={
                    "source": f"kb/{relative_path}",
                    "filename": path.name,
                },
            )
        )

    return documents
