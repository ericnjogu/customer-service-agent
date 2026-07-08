import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.config import get_settings
from app.container import create_container
from app.graph import invoke_support_graph
from app.models import IncomingMessage, SupportReply


class HealthzAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if "/healthz" in str(record.msg):
            return False

        if isinstance(record.args, dict):
            return not any("/healthz" in str(value) for value in record.args.values())

        if isinstance(record.args, tuple):
            return not any("/healthz" in str(value) for value in record.args)

        return "/healthz" not in str(record.args)


def configure_logging(log_level: str, log_format: str) -> int:
    level_name = log_level.upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level_name = "INFO"
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format=log_format,
        style="{",
        force=True,
    )
    logging.getLogger().setLevel(level)
    for logger_name in ("app", "app.container", "app.knowledge", "app.adapters.postgres"):
        logging.getLogger(logger_name).setLevel(level)

    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(HealthzAccessLogFilter())

    return level


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    effective_log_level = logging.getLevelName(
        configure_logging(settings.log_level, settings.log_format)
    )
    logging.getLogger(__name__).info(
        "Starting %s with configured_log_level=%s effective_log_level=%s",
        settings.app_name,
        settings.log_level,
        effective_log_level,
    )
    app.state.container = await create_container(settings)
    yield
    await app.state.container.close()


app = FastAPI(title="Customer Support Agent", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/synthetic", response_model=SupportReply)
async def synthetic_webhook(message: IncomingMessage, request: Request) -> SupportReply:
    return await invoke_support_graph(request.app.state.container.graph, message)
