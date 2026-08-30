import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.adapters.telegram import telegram_reply_text, telegram_update_to_incoming_message
from app.adapters.whatsapp import whatsapp_reply_text, whatsapp_update_to_incoming_messages
from app.api.onboarding_jobs import router as onboarding_jobs_router
from app.api.onboarding_sessions import router as onboarding_sessions_router
from app.api.tenants import router as tenants_router
from app.config import get_settings
from app.container import create_container
from app.graph import invoke_service_graph
from app.models import (
    ConversationRecord,
    ConversationStateUpdate,
    IncomingMessage,
    ServiceReply,
)

logger = logging.getLogger(__name__)
ERROR_ID_HEADER = "X-Error-Id"


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


def parse_cors_origins(value: str) -> list[str]:
    origins = [origin.strip() for origin in value.split(",") if origin.strip()]
    return origins or ["*"]


def new_error_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"ERR-{timestamp}-{secrets.token_hex(4).upper()}"


def internal_error_payload(error_id: str) -> dict[str, str]:
    return {
        "detail": f"An internal error has occurred. Reference: {error_id}",
        "error_id": error_id,
    }


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


app = FastAPI(title="Customer Service Agent", version="0.1.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_origins(settings.cors_allow_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(onboarding_jobs_router)
app.include_router(onboarding_sessions_router)
app.include_router(tenants_router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    headers = dict(exc.headers or {})

    if exc.status_code < 500:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=headers,
        )

    error_id = new_error_id()
    headers[ERROR_ID_HEADER] = error_id
    logger.error(
        "HTTPException returned internal error error_id=%s method=%s path=%s "
        "status_code=%s detail=%s",
        error_id,
        request.method,
        request.url.path,
        exc.status_code,
        exc.detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=internal_error_payload(error_id),
        headers=headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    error_id = new_error_id()
    logger.exception(
        "Unhandled internal error error_id=%s method=%s path=%s",
        error_id,
        request.method,
        request.url.path,
    )
    return JSONResponse(
        status_code=500,
        content=internal_error_payload(error_id),
        headers={
            ERROR_ID_HEADER: error_id,
        },
    )


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/messages/customer", response_model=ServiceReply)
async def receive_customer_message(
    message: IncomingMessage,
    request: Request,
    x_agent_tenant_id: str | None = Header(default=None),
) -> ServiceReply:
    settings = get_settings()
    return await invoke_service_graph(
        request.app.state.container.graph,
        with_tenant(message, x_agent_tenant_id, settings.default_tenant_id),
        request.app.state.container.tenant_configs,
    )


@app.post("/webhooks/synthetic", response_model=ServiceReply)
async def synthetic_webhook(
    message: IncomingMessage,
    request: Request,
    x_agent_tenant_id: str | None = Header(default=None),
) -> ServiceReply:
    settings = get_settings()
    return await invoke_service_graph(
        request.app.state.container.graph,
        with_tenant(message, x_agent_tenant_id, settings.default_tenant_id),
        request.app.state.container.tenant_configs,
    )


@app.post("/webhooks/telegram")
async def telegram_webhook(
    update: dict,
    request: Request,
    tenant_id: str | None = Query(default=None),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    x_agent_tenant_id: str | None = Header(default=None),
) -> dict:
    settings = get_settings()
    resolved_tenant_id = tenant_id or x_agent_tenant_id or settings.default_tenant_id
    telegram_credentials = await request.app.state.container.telegram_credentials.resolve(
        resolved_tenant_id
    )
    if (
        telegram_credentials.webhook_secret_token
        and x_telegram_bot_api_secret_token != telegram_credentials.webhook_secret_token
    ):
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret token")

    message = telegram_update_to_incoming_message(update)
    if message is None:
        return {"ok": True, "ignored": True}

    message = with_tenant(message, resolved_tenant_id, settings.default_tenant_id)
    reply = await invoke_service_graph(
        request.app.state.container.graph,
        message,
        request.app.state.container.tenant_configs,
    )
    telegram_sender = request.app.state.container.telegram_sender
    if telegram_sender:
        await telegram_sender.send_message(
            message.external_chat_id,
            telegram_reply_text(reply),
            tenant_id=message.tenant_id,
        )

    return {"ok": True, "reply": reply.model_dump(mode="json")}


@app.get("/webhooks/whatsapp")
async def verify_whatsapp_webhook(
    request: Request,
    tenant_id: str | None = Query(default=None),
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    x_agent_tenant_id: str | None = Header(default=None),
) -> Response:
    settings = get_settings()
    resolved_tenant_id = tenant_id or x_agent_tenant_id or settings.default_tenant_id
    whatsapp_credentials = await request.app.state.container.whatsapp_credentials.resolve(
        resolved_tenant_id
    )
    if (
        hub_mode == "subscribe"
        and hub_challenge
        and whatsapp_credentials.verify_token
        and hub_verify_token == whatsapp_credentials.verify_token
    ):
        return Response(content=hub_challenge, media_type="text/plain")

    raise HTTPException(status_code=403, detail="Invalid WhatsApp webhook verification token")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    update: dict,
    request: Request,
    tenant_id: str | None = Query(default=None),
    x_agent_tenant_id: str | None = Header(default=None),
) -> dict:
    messages = whatsapp_update_to_incoming_messages(update)
    if not messages:
        return {"ok": True, "ignored": True}

    settings = get_settings()
    replies = []
    whatsapp_sender = request.app.state.container.whatsapp_sender
    for message in messages:
        message = with_tenant(message, tenant_id or x_agent_tenant_id, settings.default_tenant_id)
        reply = await invoke_service_graph(
            request.app.state.container.graph,
            message,
            request.app.state.container.tenant_configs,
        )
        if whatsapp_sender:
            await whatsapp_sender.send_message(
                message.external_chat_id,
                whatsapp_reply_text(reply),
                tenant_id=message.tenant_id,
            )
        replies.append(reply.model_dump(mode="json"))

    return {"ok": True, "replies": replies}


@app.get("/conversations/{conversation_id}", response_model=ConversationRecord)
async def get_conversation(conversation_id: UUID, request: Request) -> ConversationRecord:
    conversation = await request.app.state.container.conversations.get_by_id(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@app.patch("/conversations/{conversation_id}/state", response_model=ConversationRecord)
async def update_conversation_state(
    conversation_id: UUID,
    update: ConversationStateUpdate,
    request: Request,
) -> ConversationRecord:
    try:
        return await request.app.state.container.conversations.update_state(
            conversation_id,
            state=update.state,
            reason=update.reason,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found") from None


def with_tenant(
    message: IncomingMessage,
    tenant_id: str | None,
    default_tenant_id: str,
) -> IncomingMessage:
    message_tenant_id = message.tenant_id.strip() or "default"
    selected_tenant_id = tenant_id or (
        default_tenant_id if message_tenant_id == "default" else message_tenant_id
    )
    normalized_tenant_id = selected_tenant_id.strip() or "default"
    if normalized_tenant_id == message.tenant_id:
        return message
    return message.model_copy(update={"tenant_id": normalized_tenant_id})
