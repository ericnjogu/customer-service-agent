"""Onboarding-owned Google consent; no bearer credentials in API responses."""

from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.adapters.google_drive import ArchiveError
from app.api.whatsapp_signup import authorize_browser
from app.whatsapp_signup import capability_hash

router = APIRouter(prefix="/onboarding", tags=["media-archive"])


def archive(request):
    service = request.app.state.container.media_archive
    if not service:
        raise HTTPException(503, "Drive archival is unavailable")
    return service


class Preference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_enabled: bool


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str = Field(min_length=32, max_length=200)
    code: SecretStr


@router.get("/sessions/{session_id}/archive")
async def status(request: Request, session_id: UUID):
    await authorize_browser(request, session_id)
    return await archive(request).status(session_id)


@router.patch("/sessions/{session_id}/archive")
async def preferences(request: Request, session_id: UUID, payload: Preference):
    await authorize_browser(request, session_id)
    service = archive(request)
    try:
        await service.preferences(session_id, payload.import_enabled)
    except ArchiveError as error:
        raise HTTPException(409, error.code) from None
    return await service.status(session_id)


@router.post("/sessions/{session_id}/drive/start")
async def start(request: Request, session_id: UUID):
    _, browser = await authorize_browser(request, session_id)
    try:
        return {"authorization_url": await archive(request).authorize(session_id, browser)}
    except ArchiveError as error:
        raise HTTPException(409, error.code) from None


@router.get("/drive/callback")
async def callback(request: Request):
    # The cross-site callback intentionally does not rely on the Strict cookie.
    # No token exchange takes place until the same-origin, cookie-authorized POST.
    params = request.query_params
    request.scope["query_string"] = b""  # redact OAuth code from server access logging
    state = params.get("state", "")
    service = archive(request)
    session = await service.pool.fetchval(
        "SELECT session_id FROM drive_oauth_attempts WHERE state_hash=$1 "
        "AND consumed_at IS NULL AND expires_at>now()",
        capability_hash(state),
    )
    if not session:
        raise HTTPException(400, "Drive authorization expired; reconnect from onboarding")
    fragment = {
        "drive_state": state,
        "drive_code": params.get("code", ""),
        "drive_error": "authorization_declined" if params.get("error") else "",
    }
    # Fragment values are never sent to nginx or application request logs.
    url = (
        service.settings.web_public_base_url.rstrip("/")
        + "/?"
        + urlencode({"session_id": str(session)})
        + "#"
        + urlencode(fragment)
    )
    return RedirectResponse(
        url,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/sessions/{session_id}/drive/complete")
async def complete(request: Request, session_id: UUID, payload: Completion):
    _, browser = await authorize_browser(request, session_id)
    service = archive(request)
    try:
        await service.complete(session_id, browser, payload.state, payload.code)
    except ArchiveError as error:
        raise HTTPException(409, error.code) from None
    return await service.status(session_id)


@router.post("/sessions/{session_id}/drive/disconnect")
async def disconnect(request: Request, session_id: UUID):
    await authorize_browser(request, session_id)
    service = archive(request)
    await service.disconnect(session_id)
    return await service.status(session_id)


@router.post("/sessions/{session_id}/archive/retry")
async def replay(request: Request, session_id: UUID):
    await authorize_browser(request, session_id)
    service = archive(request)
    await service.replay_failed(session_id)
    return await service.status(session_id)
