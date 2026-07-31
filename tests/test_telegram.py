from app.adapters.telegram import telegram_reply_text, telegram_update_to_incoming_message
from app.models import ServiceReply


def test_telegram_update_to_incoming_message_maps_text_message() -> None:
    message = telegram_update_to_incoming_message(
        {
            "update_id": 123,
            "message": {
                "message_id": 456,
                "chat": {"id": 789},
                "from": {"id": 321, "first_name": "Ada", "last_name": "Lovelace"},
                "text": "Hello",
            },
        }
    )

    assert message is not None
    assert message.event_id == "telegram:123:456"
    assert message.channel == "telegram"
    assert message.external_chat_id == "789"
    assert message.external_user_id == "321"
    assert message.sender_name == "Ada Lovelace"
    assert message.text == "Hello"


def test_telegram_update_to_incoming_message_ignores_non_text_message() -> None:
    message = telegram_update_to_incoming_message(
        {
            "update_id": 123,
            "message": {
                "message_id": 456,
                "chat": {"id": 789},
                "from": {"id": 321},
                "photo": [{"file_id": "photo-id"}],
            },
        }
    )

    assert message is None


def test_telegram_reply_text_omits_low_confidence_human_follow_up() -> None:
    text = telegram_reply_text(
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
