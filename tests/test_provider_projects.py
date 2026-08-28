import httpx
import pytest

from app.models import OnboardingProviderProjects
from app.provider_projects import (
    MetadataOnlyProviderProjectProvisioner,
    OpenAILangSmithProviderProjectProvisioner,
    is_transient_provider_error,
    retry_transient_provider_call,
)


@pytest.mark.asyncio
async def test_metadata_only_provisioning_sets_web_search_project_name() -> None:
    provisioner = MetadataOnlyProviderProjectProvisioner()

    projects = await provisioner.provision_for(
        business_name="Hustle HQ",
        website_url="https://hustlehq.example",
        provider_projects=OnboardingProviderProjects(),
    )

    assert projects.llm_project_name == "customer-service-hustle-hq"
    assert projects.langsmith_project == "customer-service-hustle-hq"
    assert projects.web_search_provider == "tavily"
    assert projects.web_search_project_name == "hustle-hq"


@pytest.mark.asyncio
async def test_api_provisioning_sets_tavily_metadata_without_key_creation(
    monkeypatch,
) -> None:
    calls = []

    async def fake_openai_project(admin_key: str, project_name: str) -> str:
        calls.append(("openai", admin_key, project_name))
        return "proj_hustle"

    async def fake_langsmith_project(**kwargs) -> None:
        calls.append(("langsmith", kwargs["project_name"]))

    monkeypatch.setattr(
        "app.provider_projects.get_or_create_openai_project",
        fake_openai_project,
    )
    monkeypatch.setattr(
        "app.provider_projects.upsert_langsmith_project",
        fake_langsmith_project,
    )

    provisioner = OpenAILangSmithProviderProjectProvisioner(
        openai_admin_key="openai-admin",
        langsmith_api_key="langsmith-key",
        langsmith_endpoint="https://smith.example",
    )

    projects = await provisioner.provision_for(
        business_name="Hustle HQ",
        website_url="https://hustlehq.example",
        provider_projects=OnboardingProviderProjects(),
    )

    assert ("openai", "openai-admin", "customer-service-hustle-hq") in calls
    assert ("langsmith", "customer-service-hustle-hq") in calls
    assert projects.web_search_provider == "tavily"
    assert projects.web_search_project_name == "hustle-hq"


@pytest.mark.asyncio
async def test_api_provisioning_reuses_existing_web_search_metadata(monkeypatch) -> None:
    async def fake_openai_project(admin_key: str, project_name: str) -> str:
        return "proj_hustle"

    async def fake_langsmith_project(**kwargs) -> None:
        return None

    monkeypatch.setattr(
        "app.provider_projects.get_or_create_openai_project",
        fake_openai_project,
    )
    monkeypatch.setattr(
        "app.provider_projects.upsert_langsmith_project",
        fake_langsmith_project,
    )

    provisioner = OpenAILangSmithProviderProjectProvisioner(
        openai_admin_key="openai-admin",
        langsmith_api_key="langsmith-key",
        langsmith_endpoint="https://smith.example",
    )

    projects = await provisioner.provision_for(
        business_name="Hustle HQ",
        website_url="https://hustlehq.example",
        provider_projects=OnboardingProviderProjects(
            web_search_provider="tavily",
            web_search_project_name="hustle-hq",
        ),
    )

    assert projects.web_search_provider == "tavily"
    assert projects.web_search_project_name == "hustle-hq"


@pytest.mark.asyncio
async def test_provider_project_retry_retries_transient_errors() -> None:
    attempts = 0

    async def flaky_operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("provider connection dropped")
        return "ok"

    result = await retry_transient_provider_call(
        "test.provider",
        flaky_operation,
        base_delay_seconds=0.25,
        max_delay_seconds=0,
    )

    assert result == "ok"
    assert attempts == 3


@pytest.mark.asyncio
async def test_provider_project_retry_does_not_retry_non_transient_status() -> None:
    attempts = 0
    request = httpx.Request("POST", "https://provider.example/projects")
    response = httpx.Response(400, request=request)

    async def bad_request_operation() -> str:
        nonlocal attempts
        attempts += 1
        raise httpx.HTTPStatusError(
            "bad request",
            request=request,
            response=response,
        )

    with pytest.raises(httpx.HTTPStatusError):
        await retry_transient_provider_call("test.provider", bad_request_operation)

    assert attempts == 1
    assert is_transient_provider_error(httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=response,
    )) is False
