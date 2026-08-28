from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Request, status

from app.models import (
    OnboardingJobAccepted,
    OnboardingJobCreate,
    OnboardingJobRecord,
    OnboardingJobRetryRequest,
)

router = APIRouter(prefix="/admin/onboarding/jobs", tags=["onboarding"])


@router.post(
    "",
    response_model=OnboardingJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_onboarding_job(
    request: Request,
    background_tasks: BackgroundTasks,
    onboarding_request: OnboardingJobCreate,
) -> OnboardingJobAccepted:
    service = request.app.state.container.onboarding_jobs
    job = await service.start_job(onboarding_request)
    background_tasks.add_task(service.process_job, job.job_id, onboarding_request)
    return OnboardingJobAccepted(job_id=job.job_id, status=job.status)


@router.get("/{job_id}", response_model=OnboardingJobRecord)
async def get_onboarding_job(
    request: Request,
    job_id: Annotated[UUID, Path()],
) -> OnboardingJobRecord:
    job = await request.app.state.container.onboarding_jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Onboarding job not found")
    return job


@router.post(
    "/{job_id}/retry",
    response_model=OnboardingJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_onboarding_job(
    request: Request,
    background_tasks: BackgroundTasks,
    retry_request: OnboardingJobRetryRequest,
    job_id: Annotated[UUID, Path()],
) -> OnboardingJobAccepted:
    service = request.app.state.container.onboarding_jobs
    try:
        job, onboarding_request = await service.retry_job(job_id, retry_request)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Onboarding job not found") from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    background_tasks.add_task(service.process_job, job.job_id, onboarding_request)
    return OnboardingJobAccepted(job_id=job.job_id, status=job.status)
