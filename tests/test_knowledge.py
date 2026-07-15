import pytest

from app.knowledge import chunk_text, load_knowledge_documents


def test_load_knowledge_documents_returns_empty_list_without_configured_path() -> None:
    assert load_knowledge_documents(None) == []


def test_load_knowledge_documents_ignores_kubernetes_configmap_backing_dirs(tmp_path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    backing_dir = knowledge_dir / "..2026_07_08_03_26_56.1899835215"
    backing_dir.mkdir()
    (backing_dir / "drinks.txt").write_text("Hidden backing drinks file", encoding="utf-8")
    (backing_dir / "menu-gpt-4.txt").write_text("Hidden backing menu file", encoding="utf-8")
    (knowledge_dir / "drinks.txt").write_text("Visible drinks file", encoding="utf-8")
    (knowledge_dir / "menu-gpt-4.txt").write_text("Visible menu file", encoding="utf-8")

    documents = load_knowledge_documents(str(knowledge_dir))

    assert [document.metadata["source"] for document in documents] == [
        "kb/drinks.txt",
        "kb/menu-gpt-4.txt",
    ]
    assert [document.page_content for document in documents] == [
        "Visible drinks file",
        "Visible menu file",
    ]
    assert [document.metadata["chunk_id"] for document in documents] == [
        "kb/drinks.txt#0000",
        "kb/menu-gpt-4.txt#0000",
    ]


def test_load_knowledge_documents_splits_large_files_into_chunks(tmp_path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "policy.txt").write_text(
        "alpha beta gamma delta epsilon zeta eta theta iota kappa",
        encoding="utf-8",
    )

    documents = load_knowledge_documents(
        str(knowledge_dir),
        chunk_size=20,
        chunk_overlap=5,
    )

    assert len(documents) > 1
    assert {document.metadata["source"] for document in documents} == {"kb/policy.txt"}
    assert [document.metadata["chunk_index"] for document in documents] == list(
        range(len(documents))
    )
    assert [document.metadata["chunk_count"] for document in documents] == [len(documents)] * len(
        documents
    )
    assert [document.metadata["chunk_id"] for document in documents] == [
        f"kb/policy.txt#{index:04d}" for index in range(len(documents))
    ]


def test_chunk_text_rejects_overlap_greater_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text("hello", chunk_size=10, chunk_overlap=10)
