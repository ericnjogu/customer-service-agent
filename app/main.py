import logging
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request

from app.adapters.telegram import telegram_reply_text, telegram_update_to_incoming_message
from app.config import get_settings
from app.container import create_container
from app.graph import invoke_support_graph
from app.models import (
    ConversationRecord,
    ConversationStatusUpdate,
    IncomingMessage,
    SupportReply,
)


class HealthzAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "/healthz" in str(record.msg):
            return False

        if isinstance(record.args, dict):
            return not any("/healthz" in str(value) for value in record.args.values())

        if isinstance(record.args, tuple):
            return not any("/healthz" in str(value) for value in record.args)

        return "/healthz" not in str(record.args)


def configure_logging(log_level: str, log_format: str) -> None:
    logging.basicConfig(
        level=log_level,
        format=log_format,
        style="{",
    )

    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(HealthzAccessLogFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    logging.getLogger(__name__).info(
        "Starting %s with configured log level=%s",
        settings.app_name,
        settings.log_level,
    )
    app.state.container = await create_container(settings)
    yield
    await app.state.container.close()


app = FastAPI(title="Customer Support Agent", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/messages/customer", response_model=SupportReply)
async def receive_customer_message(message: IncomingMessage, request: Request) -> SupportReply:
    return await invoke_support_graph(request.app.state.container.graph, message)


@app.post("/webhooks/synthetic", response_model=SupportReply)
async def synthetic_webhook(message: IncomingMessage, request: Request) -> SupportReply:
    return await receive_customer_message(message, request)


@app.post("/webhooks/telegram")
async def telegram_webhook(
    update: dict,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    settings = get_settings()
    if (
        settings.telegram_webhook_secret_token
        and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret_token
    ):
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret token")

    message = telegram_update_to_incoming_message(update)
    if message is None:
        return {"ok": True, "ignored": True}

    reply = await invoke_support_graph(request.app.state.container.graph, message)
    telegram_sender = request.app.state.container.telegram_sender
    if telegram_sender:
        await telegram_sender.send_message(message.external_chat_id, telegram_reply_text(reply))

    return {"ok": True, "reply": reply.model_dump(mode="json")}


@app.get("/conversations/{conversation_id}", response_model=ConversationRecord)
async def get_conversation(conversation_id: UUID, request: Request) -> ConversationRecord:
    conversation = await request.app.state.container.conversations.get_by_id(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@app.patch("/conversations/{conversation_id}/status", response_model=ConversationRecord)
async def update_conversation_status(
    conversation_id: UUID,
    update: ConversationStatusUpdate,
    request: Request,
) -> ConversationRecord:
    if update.status is None and update.issue_status is None:
        raise HTTPException(status_code=400, detail="At least one status field is required")

    try:
        return await request.app.state.container.conversations.update_status(
            conversation_id,
            status=update.status,
            issue_status=update.issue_status,
            reason=update.reason,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found") from None
