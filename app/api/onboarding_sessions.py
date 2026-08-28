import logging
import time
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from app.models import (
    OnboardingEmailVerificationRequest,
    OnboardingJobAccepted,
    OnboardingProviderProjects,
    OnboardingSessionCreate,
    OnboardingSessionRecord,
    OnboardingSessionUpdate,
    OnboardingSessionWebsiteRequest,
    OnboardingTelegramSetupRequest,
)
from app.onboarding_sessions import OnboardingValidationError

router = APIRouter(prefix="/onboarding/sessions", tags=["onboarding-sessions"])
logger = logging.getLogger(__name__)


@router.post(
    "",
    response_model=OnboardingSessionRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_onboarding_session(
    request: Request,
    payload: OnboardingSessionCreate,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    started_at = time.perf_counter()
    try:
        logger.info(
            "Creating onboarding session username_email=%s",
            payload.admin.username_email,
        )
        session = await service.create_session(payload)
        logger.info(
            "Created onboarding session and requested username verification "
            "session_id=%s username_email=%s status=%s current_step=%s "
            "elapsed_seconds=%.3f",
            session.session_id,
            session.admin.username_email,
            session.status,
            session.current_step,
            time.perf_counter() - started_at,
        )
        return session
    except OnboardingValidationError as error:
        logger.info(
            "Rejected onboarding session username_email=%s error=%s "
            "elapsed_seconds=%.3f",
            payload.admin.username_email,
            error,
            time.perf_counter() - started_at,
        )
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        logger.exception(
            "Failed to create onboarding session or send username verification "
            "username_email=%s elapsed_seconds=%.3f",
            payload.admin.username_email,
            time.perf_counter() - started_at,
        )
        raise


@router.get("/{session_id}", response_model=OnboardingSessionRecord)
async def get_onboarding_session(
    request: Request,
    session_id: UUID,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    session = await service.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Onboarding session not found")
    return session


@router.patch("/{session_id}", response_model=OnboardingSessionRecord)
async def update_onboarding_session(
    request: Request,
    session_id: UUID,
    payload: OnboardingSessionUpdate,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    try:
        return await service.update_session(session_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None


@router.patch("/{session_id}/website", response_model=OnboardingSessionRecord)
async def save_onboarding_session_website(
    request: Request,
    session_id: UUID,
    payload: OnboardingSessionWebsiteRequest,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    try:
        return await service.save_website(session_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except OnboardingValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/{session_id}/analyze-website", response_model=OnboardingSessionRecord)
async def analyze_onboarding_website(
    request: Request,
    session_id: UUID,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    try:
        return await service.analyze_website(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except OnboardingValidationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/{session_id}/send-username-email-verification",
    response_model=OnboardingSessionRecord,
)
async def send_username_email_verification(
    request: Request,
    session_id: UUID,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    started_at = time.perf_counter()
    try:
        logger.info("Resending onboarding username email verification session_id=%s", session_id)
        session = await service.send_username_email_verification(session_id)
        logger.info(
            "Resent onboarding username email verification session_id=%s elapsed_seconds=%.3f",
            session_id,
            time.perf_counter() - started_at,
        )
        return session
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except Exception:
        logger.exception(
            "Failed to resend onboarding username email verification session_id=%s "
            "elapsed_seconds=%.3f",
            session_id,
            time.perf_counter() - started_at,
        )
        raise


@router.post(
    "/{session_id}/send-website-email-verification",
    response_model=OnboardingSessionRecord,
)
async def send_website_email_verification(
    request: Request,
    session_id: UUID,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    started_at = time.perf_counter()
    try:
        logger.info("Resending onboarding website email verification session_id=%s", session_id)
        session = await service.send_website_email_verification(session_id)
        logger.info(
            "Resent onboarding website email verification session_id=%s elapsed_seconds=%.3f",
            session_id,
            time.perf_counter() - started_at,
        )
        return session
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except OnboardingValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception:
        logger.exception(
            "Failed to resend onboarding website email verification session_id=%s "
            "elapsed_seconds=%.3f",
            session_id,
            time.perf_counter() - started_at,
        )
        raise


@router.post("/{session_id}/verify-username-email", response_model=OnboardingSessionRecord)
async def verify_username_email(
    request: Request,
    session_id: UUID,
    payload: OnboardingEmailVerificationRequest,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    try:
        return await service.verify_username_email(session_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except OnboardingValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/{session_id}/verify-website-email", response_model=OnboardingSessionRecord)
async def verify_website_email(
    request: Request,
    session_id: UUID,
    payload: OnboardingEmailVerificationRequest,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    try:
        return await service.verify_website_email(session_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except OnboardingValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/{session_id}/request-telegram-setup", response_model=OnboardingSessionRecord)
async def request_telegram_setup(
    request: Request,
    session_id: UUID,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    try:
        return await service.prepare_telegram_setup(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except OnboardingValidationError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/{session_id}/telegram-setup", response_model=OnboardingSessionRecord)
async def save_telegram_setup(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: UUID,
    payload: OnboardingTelegramSetupRequest,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    try:
        logger.info(
            "Saving onboarding Telegram setup session_id=%s token_present=%s",
            session_id,
            bool(payload.token),
        )
        result = await service.save_telegram_setup(session_id, payload)
        if result.submission is not None:
            logger.info(
                "Scheduling onboarding job from Telegram setup session_id=%s "
                "job_id=%s idempotency_key=%s",
                session_id,
                result.submission.job.job_id,
                result.submission.job.idempotency_key,
            )
            background_tasks.add_task(
                request.app.state.container.onboarding_jobs.process_job,
                result.submission.job.job_id,
                result.submission.request,
            )
        else:
            logger.info(
                "Telegram setup saved without onboarding job submission session_id=%s",
                session_id,
            )
        return result.session
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except OnboardingValidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/{session_id}/provider-projects", response_model=OnboardingSessionRecord)
async def save_provider_projects(
    request: Request,
    session_id: UUID,
    payload: OnboardingProviderProjects,
) -> OnboardingSessionRecord:
    service = request.app.state.container.onboarding_sessions
    try:
        return await service.save_provider_projects(session_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None


@router.post(
    "/{session_id}/submit",
    response_model=OnboardingJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_onboarding_session(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: UUID,
) -> OnboardingJobAccepted:
    service = request.app.state.container.onboarding_sessions
    try:
        submission = await service.submit_session(session_id)
        background_tasks.add_task(
            request.app.state.container.onboarding_jobs.process_job,
            submission.job.job_id,
            submission.request,
        )
        return OnboardingJobAccepted(
            job_id=submission.job.job_id,
            status=submission.job.status,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Onboarding session not found") from None
    except OnboardingValidationError as error:
        await service.mark_session_failed(session_id, str(error))
        raise HTTPException(status_code=409, detail=str(error)) from error
