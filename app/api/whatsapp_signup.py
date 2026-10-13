import json
import secrets
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.adapters.meta_signup import MetaSignupError
from app.config import get_settings
from app.models import OnboardingJobCreate
from app.whatsapp_signup import WhatsAppSignupError, capability_hash

router = APIRouter(prefix="/onboarding", tags=["whatsapp-signup"])


def browser_cookie(session_id: UUID) -> str:
    return f"onboarding-{session_id}"


def service_for(request: Request, *, require_enabled=True):
    settings = get_settings()
    service = request.app.state.container.whatsapp_signup
    if not service or (require_enabled and not settings.onboarding_whatsapp_enabled):
        raise HTTPException(503, "WhatsApp signup is not available yet")
    return service


async def authorize_browser(request: Request, session_id: UUID):
    service = service_for(request, require_enabled=False)
    browser = request.cookies.get(browser_cookie(session_id), "")
    try:
        await service.require_browser(session_id, browser)
    except WhatsAppSignupError:
        raise HTTPException(
            403,
            "Verify your account email in this browser before continuing",
            headers={"X-Onboarding-Error": "browser_verification_required"},
        ) from None
    if request.method != "GET":
        public = urlsplit(get_settings().web_public_base_url)
        if request.headers.get("origin") != f"{public.scheme}://{public.netloc}":
            raise HTTPException(403, "Invalid onboarding request origin")
    return service, browser


@router.get("/config")
async def public_configuration():
    settings = get_settings()
    return {
        "telegram_enabled": settings.onboarding_telegram_enabled,
        "whatsapp_enabled": settings.onboarding_whatsapp_enabled,
        "whatsapp_mode": settings.onboarding_whatsapp_mode,
        "whatsapp_transport": "redirect",
        "meta_app_id": settings.meta_app_id,
        "meta_signup_configuration_id": settings.meta_signup_configuration_id,
        "meta_graph_api_version": settings.meta_graph_api_version,
    }


class SignupCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: UUID
    state: str = Field(min_length=32, max_length=200)
    code: SecretStr
    waba_id: str = Field(pattern=r"^[0-9]{1,32}$")
    phone_number_id: str = Field(pattern=r"^[0-9]{1,32}$")


class SignupCancellation(BaseModel):
    attempt_id: UUID
    state: str = Field(min_length=32, max_length=200)


class RedirectCompletion(SignupCancellation):
    model_config = ConfigDict(extra="forbid")
    code: SecretStr | None = None
    declined: bool = False


def meta_redirect_uri():
    return get_settings().web_public_base_url.rstrip("/") + "/api/onboarding/whatsapp/callback"


@router.post("/sessions/{session_id}/whatsapp/redirect/start")
async def redirect_start(request: Request, session_id: UUID):
    service_for(request)
    service, browser = await authorize_browser(request, session_id)
    settings = get_settings()
    try:
        attempt = await service.start(session_id, browser, redirect_uri=meta_redirect_uri())
    except WhatsAppSignupError as error:
        raise HTTPException(409, str(error)) from None
    extras = {"setup": {}, "version": "v4", "sessionInfoVersion": "3"}
    if settings.onboarding_whatsapp_mode == "coexistence":
        extras["featureType"] = "whatsapp_business_app_onboarding"
    query = urlencode(
        {
            "client_id": settings.meta_app_id,
            "config_id": settings.meta_signup_configuration_id,
            "redirect_uri": meta_redirect_uri(),
            "state": attempt["state"],
            "response_type": "code",
            "override_default_response_type": "true",
            "display": "page",
            "extras": json.dumps(extras),
        }
    )
    return {
        "authorization_url": f"https://www.facebook.com/{settings.meta_graph_api_version}/dialog/oauth?{query}"
    }


@router.get("/whatsapp/callback", response_class=HTMLResponse)
async def redirect_callback(request: Request):
    params = request.query_params
    request.scope["query_string"] = b""  # Never write OAuth codes to access logs.
    state, code = params.get("state", ""), params.get("code", "")
    if not 32 <= len(state) <= 200 or len(code) > 8192:
        raise HTTPException(400, "Invalid WhatsApp return; retry from onboarding")
    service = service_for(request)
    attempt = await service.repository.pool.fetchrow(
        "SELECT attempt_id,session_id FROM whatsapp_signup_attempts WHERE state_hash=$1 "
        "AND redirect_uri=$2 AND consumed_at IS NULL AND expires_at>now()",
        capability_hash(state),
        meta_redirect_uri(),
    )
    if not attempt:
        raise HTTPException(400, "WhatsApp authorization expired; retry from onboarding")
    session_id = str(attempt["session_id"])
    payload = (
        json.dumps(
            {
                "attempt_id": str(attempt["attempt_id"]),
                "state": state,
                "code": code or None,
                "declined": bool(params.get("error")) or not code,
            }
        )
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    nonce = secrets.token_urlsafe(24)
    completion_path = f"/api/onboarding/sessions/{session_id}/whatsapp/redirect/complete"
    # Load a document on our origin before POSTing, so SameSite=Strict remains intact.
    # No external scripts, persistent browser storage, provider errors or token exchange on GET.
    html = f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Connecting WhatsApp</title></head>
<body><p role="status">Completing your WhatsApp connection…</p>
<noscript>JavaScript is required. Return to onboarding and retry.</noscript>
<script nonce="{nonce}">
history.replaceState(null, "", location.pathname);
(async () => {{
  let result = "failed";
  try {{
    const response = await fetch("{completion_path}", {{
      method: "POST", credentials: "same-origin", headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({payload})
    }});
    if (response.ok) result = (await response.json()).declined ? "cancelled" : "connected";
    else if (response.status === 403) result = "browser_required";
    else if ((await response.json()).detail === "meta_asset_selection_ambiguous") {{
      result = "ambiguous";
    }}
  }} catch {{}}
  location.replace("/?session_id={session_id}&meta_result=" + result);
}})();
</script></body></html>'''
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/sessions/{session_id}/whatsapp/redirect/complete")
async def redirect_complete(request: Request, session_id: UUID, payload: RedirectCompletion):
    service_for(request)
    service, browser = await authorize_browser(request, session_id)
    try:
        if payload.declined:
            await service.cancel(
                session_id, browser, attempt_id=payload.attempt_id, state=payload.state
            )
            return {"declined": True}
        if not payload.code or not payload.code.get_secret_value():
            raise WhatsAppSignupError("meta_authorization_missing")
        result = await service.complete(
            session_id,
            browser,
            attempt_id=payload.attempt_id,
            state=payload.state,
            code=payload.code,
            redirect_uri=meta_redirect_uri(),
        )
    except (WhatsAppSignupError, MetaSignupError) as error:
        try:
            await service.cancel(
                session_id, browser, attempt_id=payload.attempt_id, state=payload.state
            )
        except WhatsAppSignupError:
            pass  # Invalid/foreign attempts cannot unlock another browser's settings.
        raise HTTPException(409, str(error)) from None
    return connection_status(result)


@router.post("/sessions/{session_id}/whatsapp/cancel")
async def cancel(request: Request, session_id: UUID, payload: SignupCancellation):
    service, browser = await authorize_browser(request, session_id)
    try:
        return await service.cancel(session_id, browser, **payload.model_dump())
    except WhatsAppSignupError as error:
        raise HTTPException(409, str(error)) from None


@router.post("/sessions/{session_id}/whatsapp/start")
async def start(request: Request, session_id: UUID):
    service_for(request)
    service, browser = await authorize_browser(request, session_id)
    return await service.start(session_id, browser)


@router.post("/sessions/{session_id}/whatsapp/complete")
async def complete(request: Request, session_id: UUID, payload: SignupCompletion):
    service_for(request)
    service, browser = await authorize_browser(request, session_id)
    try:
        result = await service.complete(session_id, browser, **payload.model_dump())
    except (WhatsAppSignupError, MetaSignupError) as error:
        raise HTTPException(409, str(error)) from None
    return connection_status(result)


def connection_status(result):
    if not result:
        return {"status": "not_connected", "display_number": None, "error_code": None}
    return {
        "status": result.status,
        "display_number": result.display_number,
        "error_code": result.error_code,
    }


@router.get("/sessions/{session_id}/whatsapp/status")
async def status(request: Request, session_id: UUID):
    service, _ = await authorize_browser(request, session_id)
    return connection_status(await service.repository.for_session(session_id))


@router.get("/sessions/{session_id}/provisioning")
async def provisioning_status(request: Request, session_id: UUID):
    await authorize_browser(request, session_id)
    session = await request.app.state.container.onboarding_sessions.get_session(session_id)
    if not session or not session.submitted_job_id:
        raise HTTPException(404, "No provisioning job for this session")
    job = await request.app.state.container.onboarding_jobs.get_job(session.submitted_job_id)
    return {
        "status": job.status,
        "job_id": job.job_id,
        "error": "Provisioning failed. Please retry." if job.status == "failed" else None,
    }


@router.post("/sessions/{session_id}/provisioning/retry")
async def retry_provisioning(request: Request, session_id: UUID, background_tasks: BackgroundTasks):
    await authorize_browser(request, session_id)
    container = request.app.state.container
    session = await container.onboarding_sessions.get_session(session_id)
    if not session or not session.submitted_job_id:
        raise HTTPException(404, "No provisioning job for this session")
    job = await container.onboarding_jobs.get_job(session.submitted_job_id)
    payload = await container.onboarding.get_job_payload(job.job_id)
    connection = await container.whatsapp_connections.for_session(session_id)
    if not connection or connection.status != "connected":
        raise HTTPException(409, "A connected WhatsApp number is required")
    model = OnboardingJobCreate.model_validate(payload)
    if model.whatsapp_connection_id != connection.connection_id:
        raise HTTPException(409, "Provisioning connection mismatch")
    async with container.whatsapp_connections.provisioning_lock(job.job_id) as acquired:
        if not acquired:
            raise HTTPException(409, "Provisioning is still running; please wait")
        current = await container.onboarding_jobs.get_job(job.job_id)
        if current.status not in {"failed", "running", "accepted"}:
            raise HTTPException(409, "Provisioning cannot be retried in its current state")
        await container.onboarding.mark_job_accepted(job.job_id)
    background_tasks.add_task(container.onboarding_jobs.process_job, job.job_id, model)
    return {"status": "accepted", "job_id": job.job_id}
