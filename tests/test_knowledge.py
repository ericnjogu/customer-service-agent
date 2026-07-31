import pytest

from app.knowledge import chunk_text


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
