from app.whatsapp_coexistence import imported_messages


def test_bsuid_history_context_preserves_provenance_and_excludes_unrelated_messages():
    common = {"timestamp": "1700000000", "type": "text", "text": {"body": "Hello"}}
    value = {"history": [{"threads": [{
        "context": {"user_id": "NL.Abc123"},
        "messages": [
            {**common, "id": "customer", "from_user_id": "NL.Abc123"},
            {**common, "id": "agent", "from": "254700000002", "to_user_id": "NL.Abc123"},
            {**common, "id": "unrelated", "from_user_id": "NL.Other"},
        ],
    }]}]}
    records = list(imported_messages(value, history=True))
    assert [(r.chat_id, r.sender_type) for r in records] == [
        ("NL.Abc123", "CUSTOMER"), ("NL.Abc123", "AGENT"),
    ]


def test_bsuid_media_and_human_echo_share_chat_without_interpretation():
    common = {"id": "media", "timestamp": "1700000000", "type": "image",
              "image": {"id": "media-id"}}
    (incoming,) = imported_messages(
        {"messages": [{**common, "from_user_id": "NL.Abc123"}]}, history=False, incoming=True
    )
    (echo,) = imported_messages(
        {"message_echoes": [{**common, "to_user_id": "NL.Abc123"}]}, history=False
    )
    assert incoming.chat_id == echo.chat_id == "NL.Abc123"
    assert incoming.sender_type == "CUSTOMER"
    assert echo.sender_type == "AGENT"
    assert incoming.body == echo.body == "[WhatsApp image message]"


def test_history_preserves_customer_and_human_provenance():
    value = {
        "history": [
            {
                "threads": [
                    {
                        "id": "254700000001",
                        "messages": [
                            {
                                "id": "incoming",
                                "from": "254700000001",
                                "timestamp": "1700000000",
                                "type": "text",
                                "text": {"body": "My order is late"},
                            },
                            {
                                "id": "outgoing",
                                "from": "254700000002",
                                "to": "254700000001",
                                "timestamp": "1700000010",
                                "type": "text",
                                "text": {"body": "We will check"},
                            },
                        ],
                    }
                ]
            }
        ]
    }
    records = list(imported_messages(value, history=True))
    assert [r.sender_type for r in records] == ["CUSTOMER", "AGENT"]
    assert all(r.is_history for r in records)
    assert records[0].sent_at < records[1].sent_at


def test_echo_is_human_even_when_text_is_missing():
    value = {
        "message_echoes": [
            {
                "id": "echo",
                "to": "254700000001",
                "timestamp": "1700000000",
                "type": "image",
                "image": {"id": "media"},
            }
        ]
    }
    (record,) = imported_messages(value, history=False)
    assert record.sender_type == "AGENT"
    assert record.body == "[WhatsApp image message]"
    assert not record.is_history


def test_malformed_echoes_are_not_imported():
    assert (
        list(imported_messages({"message_echoes": [None, {}, {"to": "invalid"}]}, history=False))
        == []
    )
