import json

import httpx
import pytest

from app.adapters.whatsapp import (
    WhatsAppCloudClient,
    whatsapp_reply_text,
    whatsapp_update_to_incoming_messages,
)
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


@pytest.mark.parametrize("identifier,key", [
    ("NL.Abc123", "from_user_id"),
    ("NL.ENT.Abc123", "from_parent_user_id"),
])
def test_bsuid_only_message_preserves_name_and_identity(identifier, key):
    value = {
        "contacts": [{"user_id": "NL.Abc123", "parent_user_id": "NL.ENT.Abc123",
                      "profile": {"name": "Ada"}}],
        "messages": [{key: identifier, "id": "wamid.bsuid", "type": "text",
                      "text": {"body": "Hello"}}],
    }
    update = {"entry": [{"changes": [{"value": value}]}]}
    (message,) = whatsapp_update_to_incoming_messages(update)
    assert message.external_chat_id == identifier
    assert message.external_user_id == identifier
    assert message.sender_name == "Ada"
    value["messages"][0]["from"] = "254700000001"
    assert whatsapp_update_to_incoming_messages(update)[0].external_chat_id == "254700000001"


@pytest.mark.parametrize("identifier", [None, {}, "", "NL.bad/id", "NL." + "a" * 129])
def test_invalid_sender_is_skipped_without_logging_payload(identifier, caplog):
    update = {"entry": [{"changes": [{"value": {"messages": [{
        "from_user_id": identifier, "id": "wamid.private", "type": "text",
        "text": {"body": "private message"},
    }]}}]}]}
    assert whatsapp_update_to_incoming_messages(update) == []
    assert "missing or invalid sender/message identity" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize("identifier,key", [
    ("254700000001", "to"), ("NL.Abc123", "recipient"),
    ("NL.ENT.Abc123", "recipient"),
])
async def test_cloud_send_uses_correct_recipient_field(monkeypatch, identifier, key):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "sent"}]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(handle)
    ))
    await WhatsAppCloudClient("test-token", "phone", "v25.0").send_message(identifier, "Hello")
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "messaging_product": "whatsapp", key: identifier,
        "type": "text", "text": {"body": "Hello"},
    }


async def test_invalid_outbound_identity_fails_before_network():
    with pytest.raises(ValueError, match="Invalid WhatsApp recipient"):
        await WhatsAppCloudClient("test-token", "phone").send_message("not-an-id", "Hello")
