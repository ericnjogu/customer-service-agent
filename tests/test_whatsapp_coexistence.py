from app.whatsapp_coexistence import imported_messages


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
