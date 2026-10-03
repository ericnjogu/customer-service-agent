from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.adapters.meta_signup import MetaSignupError
from app.config import get_settings
from app.models import OnboardingJobCreate
from app.whatsapp_signup import WhatsAppSignupError

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
            403, "Verify your account email in this browser before continuing"
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
