from app.knowledge import load_knowledge_documents


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
