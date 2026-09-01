import pytest

from app.knowledge import chunk_text, chunk_text_with_metadata


def test_chunk_text_splits_large_text() -> None:
    chunks = chunk_text(
        "alpha beta gamma delta epsilon zeta eta theta iota kappa",
        chunk_size=20,
        chunk_overlap=5,
    )

    assert len(chunks) > 1
    assert all(chunk.strip() for chunk in chunks)


def test_chunk_text_rejects_overlap_greater_than_chunk_size() -> None:
    with pytest.raises(ValueError, match="chunk_overlap"):
        chunk_text("hello", chunk_size=10, chunk_overlap=10)


def test_chunk_text_preserves_markdown_section_titles() -> None:
    chunks = chunk_text_with_metadata(
        "\n".join(
            [
                "# Menu",
                "",
                "Chapati and tea are available.",
                "",
                "## Drinks",
                "",
                "Mango juice and coffee are available.",
            ]
        ),
        chunk_size=80,
        chunk_overlap=10,
    )

    assert [chunk.section_title for chunk in chunks] == ["Menu", "Drinks"]
    assert chunks[0].content.startswith("# Menu")
    assert chunks[1].content.startswith("## Drinks")
    assert all(chunk.chunk_count == 2 for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert all(len(chunk.content_hash) == 64 for chunk in chunks)


def test_chunk_text_repeats_section_title_for_split_section() -> None:
    chunks = chunk_text_with_metadata(
        "# Refund policy\n\n" + "Refunds are available before dispatch. " * 20,
        chunk_size=120,
        chunk_overlap=20,
    )

    assert len(chunks) > 1
    assert all(chunk.section_title == "Refund policy" for chunk in chunks)
    assert all("Refund policy" in chunk.content for chunk in chunks)
