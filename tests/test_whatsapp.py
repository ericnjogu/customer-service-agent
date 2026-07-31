from app.adapters.whatsapp import whatsapp_reply_text, whatsapp_update_to_incoming_messages
from app.models import ServiceReply


def test_whatsapp_update_to_incoming_messages_maps_text_message() -> None:
    messages = whatsapp_update_to_incoming_messages(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [
                                    {
                                        "wa_id": "254700000001",
                                        "profile": {"name": "Ada Lovelace"},
                                    }
                                ],
                                "messages": [
                                    {
                                        "from": "254700000001",
                                        "id": "wamid.abc123",
                                        "type": "text",
                                        "text": {"body": "Hello"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.event_id == "whatsapp:wamid.abc123"
    assert message.channel == "whatsapp"
    assert message.external_chat_id == "254700000001"
    assert message.external_user_id == "254700000001"
    assert message.sender_name == "Ada Lovelace"
    assert message.text == "Hello"


def test_whatsapp_update_to_incoming_messages_ignores_non_text_message() -> None:
    messages = whatsapp_update_to_incoming_messages(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "254700000001",
                                        "id": "wamid.abc123",
                                        "type": "image",
                                        "image": {"id": "media-id"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    )

    assert messages == []


def test_whatsapp_reply_text_omits_low_confidence_human_follow_up() -> None:
    text = whatsapp_reply_text(
        ServiceReply(
            conversation_id="00000000-0000-0000-0000-000000000000",
            answer="I could not answer that.",
            confidence=0.0,
            citations=[],
            low_confidence=True,
            state="BOT_ACTIVE",
        )
    )

    assert text == "I could not answer that."
