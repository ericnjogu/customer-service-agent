from typing import Protocol

import httpx

from app.models import IncomingMessage, SupportReply


class WhatsAppSender(Protocol):
    async def send_message(self, to: str, text: str) -> None: ...


class WhatsAppCloudClient:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        graph_api_version: str = "v20.0",
    ) -> None:
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.graph_api_version = graph_api_version

    async def send_message(self, to: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                (
                    f"https://graph.facebook.com/{self.graph_api_version}/"
                    f"{self.phone_number_id}/messages"
                ),
                headers={"Authorization": f"Bearer {self.access_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            response.raise_for_status()


def whatsapp_update_to_incoming_messages(update: dict) -> list[IncomingMessage]:
    messages: list[IncomingMessage] = []

    for entry in update.get("entry", []):
        if not isinstance(entry, dict):
            continue

        for change in entry.get("changes", []):
            if not isinstance(change, dict):
                continue

            value = change.get("value")
            if not isinstance(value, dict):
                continue

            contact_names = whatsapp_contact_names_by_id(value)
            for message in value.get("messages", []):
                incoming = whatsapp_message_to_incoming_message(
                    message,
                    contact_names=contact_names,
                )
                if incoming:
                    messages.append(incoming)

    return messages


def whatsapp_message_to_incoming_message(
    message: dict,
    *,
    contact_names: dict[str, str],
) -> IncomingMessage | None:
    if not isinstance(message, dict):
        return None

    from_id = message.get("from")
    message_id = message.get("id")
    if from_id is None or message_id is None:
        return None

    text = message.get("text")
    if not isinstance(text, dict):
        return None

    body = text.get("body")
    if not isinstance(body, str) or not body.strip():
        return None

    external_id = str(from_id)
    return IncomingMessage(
        event_id=f"whatsapp:{message_id}",
        channel="whatsapp",
        external_chat_id=external_id,
        external_user_id=external_id,
        sender_name=contact_names.get(external_id),
        text=body,
    )


def whatsapp_contact_names_by_id(value: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for contact in value.get("contacts", []):
        if not isinstance(contact, dict):
            continue

        wa_id = contact.get("wa_id")
        profile = contact.get("profile")
        if wa_id is None or not isinstance(profile, dict):
            continue

        name = profile.get("name")
        if isinstance(name, str) and name.strip():
            names[str(wa_id)] = name.strip()

    return names


def whatsapp_reply_text(reply: SupportReply) -> str:
    return reply.answer
