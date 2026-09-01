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


def test_chunk_text_uses_recursive_splitting_without_heading_metadata() -> None:
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

    assert all(chunk.section_title is None for chunk in chunks)
    assert chunks[0].content.startswith("# Menu")
    assert all(chunk.chunk_count == len(chunks) for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert all(len(chunk.content_hash) == 64 for chunk in chunks)


def test_chunk_text_does_not_repeat_heading_for_split_chunks() -> None:
    chunks = chunk_text_with_metadata(
        "# Refund policy\n\n" + "Refunds are available before dispatch. " * 20,
        chunk_size=120,
        chunk_overlap=20,
    )

    assert len(chunks) > 1
    assert all(chunk.section_title is None for chunk in chunks)
    assert chunks[0].content.startswith("# Refund policy")
    assert any("Refund policy" not in chunk.content for chunk in chunks[1:])
