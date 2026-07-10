import pytest

from app.adapters.postgres import decode_metadata, local_embedding


def dot(left: list[float], right: list[float]) -> float:
    return sum(
        left_value * right_value
        for left_value, right_value in zip(left, right, strict=True)
    )


def test_local_embedding_ignores_stop_words() -> None:
    assert local_embedding("how do i") == pytest.approx(local_embedding(""))


def test_local_embedding_aligns_query_terms_with_document_terms() -> None:
    query = local_embedding("How do I reset my password?")
    relevant_document = local_embedding("Reset password from Settings and Security.")
    unrelated_document = local_embedding("Refund requests require an order number.")

    assert dot(query, relevant_document) > dot(query, unrelated_document)


def test_decode_metadata_accepts_dict_or_json_string() -> None:
    assert decode_metadata({"source": "kb/refunds.txt"}) == {"source": "kb/refunds.txt"}
    assert decode_metadata('{"source": "kb/refunds.txt"}') == {"source": "kb/refunds.txt"}
