from typing import Protocol

import httpx

from app.models import IncomingMessage, SupportReply


class TelegramSender(Protocol):
    async def send_message(self, chat_id: str, text: str) -> None: ...


class TelegramBotClient:
    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token

    async def send_message(self, chat_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            response.raise_for_status()


def telegram_update_to_incoming_message(update: dict) -> IncomingMessage | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("id") is None:
        return None

    sender = message.get("from")
    if not isinstance(sender, dict) or sender.get("id") is None:
        return None

    update_id = str(update.get("update_id", "unknown-update"))
    message_id = str(message.get("message_id", "unknown-message"))
    sender_name = format_sender_name(sender)

    return IncomingMessage(
        event_id=f"telegram:{update_id}:{message_id}",
        channel="telegram",
        external_chat_id=str(chat["id"]),
        external_user_id=str(sender["id"]),
        sender_name=sender_name,
        text=text,
    )


def format_sender_name(sender: dict) -> str | None:
    names = [
        str(sender.get("first_name", "")).strip(),
        str(sender.get("last_name", "")).strip(),
    ]
    full_name = " ".join(name for name in names if name)
    if full_name:
        return full_name

    username = sender.get("username")
    return str(username) if username else None


def telegram_reply_text(reply: SupportReply) -> str:
    return reply.answer
