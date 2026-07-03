from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.config import get_settings
from app.container import create_container
from app.graph import invoke_support_graph
from app.models import IncomingMessage, SupportReply


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.container = await create_container(get_settings())
    yield
    await app.state.container.close()


app = FastAPI(title="Customer Support Agent", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/synthetic", response_model=SupportReply)
async def synthetic_webhook(message: IncomingMessage, request: Request) -> SupportReply:
    return await invoke_support_graph(request.app.state.container.graph, message)
