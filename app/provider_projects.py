import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from langsmith import Client as LangSmithClient
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt
from tenacity.wait import wait_random_exponential

from app.models import OnboardingProviderProjects, OnboardingSessionRecord
from app.tenancy import tenant_slug

logger = logging.getLogger(__name__)


class ProviderProjectProvisioningError(RuntimeError):
    pass


TRANSIENT_HTTP_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


def default_provider_project_name_for_business(business_name: str) -> str:
    return f"customer-service-{tenant_slug(business_name)}"


def default_provider_project_name(session: OnboardingSessionRecord) -> str:
    business_name = (
        session.business_profile.business_name
        if session.business_profile
        else str(session.website_url)
    )
    return default_provider_project_name_for_business(business_name)


class MetadataOnlyProviderProjectProvisioner:
    async def provision(
        self,
        session: OnboardingSessionRecord,
    ) -> OnboardingProviderProjects:
        return await self.provision_for(
            business_name=(
                session.business_profile.business_name
                if session.business_profile
                else str(session.website_url)
            ),
            website_url=str(session.website_url),
            provider_projects=session.provider_projects,
            session_id=session.session_id,
        )

    async def provision_for(
        self,
        *,
        business_name: str,
        website_url: str,
        provider_projects: OnboardingProviderProjects,
        session_id: UUID | None = None,
    ) -> OnboardingProviderProjects:
        project_name = default_provider_project_name_for_business(business_name)
        web_search_project_name = tenant_slug(business_name)
        existing = provider_projects
        return existing.model_copy(
            update={
                "llm_project_name": existing.llm_project_name or project_name,
                "langsmith_project": existing.langsmith_project or project_name,
                "web_search_provider": existing.web_search_provider or "tavily",
                "web_search_project_name": (
                    existing.web_search_project_name or web_search_project_name
                ),
            }
        )


@dataclass
class OpenAILangSmithProviderProjectProvisioner:
    openai_admin_key: str
    langsmith_api_key: str
    langsmith_endpoint: str
    langsmith_workspace_id: str | None = None

    async def provision(
        self,
        session: OnboardingSessionRecord,
    ) -> OnboardingProviderProjects:
        return await self.provision_for(
            business_name=(
                session.business_profile.business_name
                if session.business_profile
                else str(session.website_url)
            ),
            website_url=str(session.website_url),
            provider_projects=session.provider_projects,
            session_id=session.session_id,
        )

    async def provision_for(
        self,
        *,
        business_name: str,
        website_url: str,
        provider_projects: OnboardingProviderProjects,
        session_id: UUID | None = None,
    ) -> OnboardingProviderProjects:
        project_name = default_provider_project_name_for_business(business_name)
        existing = provider_projects
        llm_project_name = existing.llm_project_name or project_name
        langsmith_project = existing.langsmith_project or project_name
        web_search_project_name = existing.web_search_project_name or tenant_slug(
            business_name
        )
        logger.info(
            "Provisioning tenant provider projects session_id=%s "
            "llm_project_name=%s langsmith_project=%s web_search_project_name=%s",
            session_id,
            llm_project_name,
            langsmith_project,
            web_search_project_name,
        )

        llm_project_id = existing.llm_project_id or await retry_transient_provider_call(
            "openai.project.get_or_create",
            lambda: get_or_create_openai_project(
                self.openai_admin_key,
                llm_project_name,
            ),
        )
        await retry_transient_provider_call(
            "langsmith.project.upsert",
            lambda: upsert_langsmith_project(
                api_key=self.langsmith_api_key,
                endpoint=self.langsmith_endpoint,
                workspace_id=self.langsmith_workspace_id,
                project_name=langsmith_project,
                session_id=session_id,
                website_url=website_url,
            ),
        )
        logger.info(
            "Provisioned tenant provider projects session_id=%s "
            "llm_project_id=%s llm_project_name=%s langsmith_project=%s "
            "web_search_provider=tavily web_search_project_name=%s",
            session_id,
            llm_project_id,
            llm_project_name,
            langsmith_project,
            web_search_project_name,
        )
        return OnboardingProviderProjects(
            llm_project_id=llm_project_id,
            llm_project_name=llm_project_name,
            langsmith_project=langsmith_project,
            web_search_provider="tavily",
            web_search_project_name=web_search_project_name,
        )


async def get_or_create_openai_project(admin_key: str, project_name: str) -> str:
    headers = {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        project_id = await find_openai_project(client, headers, project_name)
        if project_id:
            logger.info("Found existing OpenAI project name=%s id=%s", project_name, project_id)
            return project_id

        logger.info("Creating OpenAI project name=%s", project_name)
        response = await client.post(
            "https://api.openai.com/v1/organization/projects",
            headers=headers,
            json={"name": project_name},
        )
        response.raise_for_status()
        payload = response.json()
        project_id = str(payload.get("id") or "")
        if not project_id:
            raise ProviderProjectProvisioningError(
                "OpenAI project create response did not include an id"
            )
        return project_id


async def find_openai_project(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    project_name: str,
) -> str | None:
    after: str | None = None
    while True:
        params: dict[str, Any] = {
            "limit": 100,
            "include_archived": "false",
        }
        if after:
            params["after"] = after
        response = await client.get(
            "https://api.openai.com/v1/organization/projects",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        for project in payload.get("data") or []:
            if project.get("name") == project_name and project.get("status") == "active":
                return str(project.get("id") or "")
        if not payload.get("has_more"):
            return None
        after = payload.get("last_id")
        if not after:
            return None


async def upsert_langsmith_project(
    *,
    api_key: str,
    endpoint: str,
    workspace_id: str | None,
    project_name: str,
    session_id: UUID | None,
    website_url: str,
) -> None:
    def create_project() -> None:
        client = LangSmithClient(
            api_key=api_key,
            api_url=endpoint,
            workspace_id=workspace_id,
        )
        client.create_project(
            project_name,
            description=f"Tracing project for {project_name}",
            metadata={
                "source": "customer-service-onboarding",
                "onboarding_session_id": str(session_id) if session_id else "",
                "website_url": website_url,
            },
            upsert=True,
        )

    logger.info("Creating/upserting LangSmith project name=%s", project_name)
    await asyncio.to_thread(create_project)


async def retry_transient_provider_call(
    operation_name: str,
    operation,
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 4.0,
) -> Any:
    retrying = AsyncRetrying(
        retry=retry_if_exception(is_transient_provider_error),
        wait=wait_random_exponential(
            multiplier=base_delay_seconds,
            max=max_delay_seconds,
        ),
        stop=stop_after_attempt(max_attempts),
        before_sleep=lambda retry_state: log_provider_retry(
            operation_name,
            retry_state,
            max_attempts=max_attempts,
        ),
        reraise=True,
    )
    async for attempt in retrying:
        with attempt:
            return await operation()
    raise ProviderProjectProvisioningError(
        f"Provider project operation did not run: {operation_name}"
    )


def log_provider_retry(
    operation_name: str,
    retry_state: RetryCallState,
    *,
    max_attempts: int,
) -> None:
    error = retry_state.outcome.exception() if retry_state.outcome else None
    delay = retry_state.next_action.sleep if retry_state.next_action else 0
    logger.warning(
        "Transient provider project operation failed; retrying "
        "operation=%s attempt=%s max_attempts=%s delay_seconds=%.2f error=%s",
        operation_name,
        retry_state.attempt_number,
        max_attempts,
        delay,
        error,
    )


def is_transient_provider_error(error: Exception) -> bool:
    if isinstance(error, httpx.TimeoutException | httpx.NetworkError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in TRANSIENT_HTTP_STATUS_CODES
    return False
