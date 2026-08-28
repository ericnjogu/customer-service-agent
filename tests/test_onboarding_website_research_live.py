import logging
import os

import pytest

from app.adapters.llm import OpenAIWebsiteAnalyzer
from app.config import Settings
from app.container import create_platform_website_researcher
from app.models import OnboardingAdmin, OnboardingSessionRecord, WebsiteResearchResult

logger = logging.getLogger(__name__)

pytestmark = pytest.mark.skipif(
    os.getenv("AGENT_RUN_LIVE_OPENAI_TESTS", "").lower() not in {"1", "true", "yes"},
    reason="set AGENT_RUN_LIVE_OPENAI_TESTS=true to run live OpenAI integration tests",
)


class LoggingCachedWebsiteResearcher:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.cached_result: WebsiteResearchResult | None = None

    async def research(self, website_url: str) -> WebsiteResearchResult:
        if self.cached_result is None:
            self.cached_result = await self.delegate.research(website_url)
            logger.info(
                "Live website research output website_url=%s provider=%s "
                "source_count=%s chars=%s\n%s",
                website_url,
                type(self.delegate).__name__,
                len(self.cached_result.sources),
                len(self.cached_result.notes),
                self.cached_result.model_dump_json(indent=2),
            )
        else:
            logger.info(
                "Reusing cached live website research output website_url=%s "
                "source_count=%s chars=%s",
                website_url,
                len(self.cached_result.sources),
                len(self.cached_result.notes),
            )
        return self.cached_result


def expected_contact_fragments() -> list[str]:
    configured = os.getenv("OPENAI_WEBSITE_RESEARCH_EXPECTED_SOCIAL_URLS")
    if configured:
        return [
            fragment.strip().lower()
            for fragment in configured.split(",")
            if fragment.strip()
        ]
    return ["facebook.com"]


@pytest.mark.asyncio
async def test_live_hybrid_website_analysis_finds_expected_contact_links() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY is required for live OpenAI integration tests")

    settings = Settings()
    provider = settings.platform_web_search_provider.lower()
    if provider != "tavily":
        pytest.skip(
            "set AGENT_PLATFORM_WEB_SEARCH_PROVIDER=tavily to run this live test"
        )

    if not settings.platform_web_search_api_key:
        pytest.skip(
            "AGENT_PLATFORM_WEB_SEARCH_API_KEY or TAVILY_API_KEY is required "
            "for live website research"
        )

    from openai import AsyncOpenAI

    website_url = os.getenv("OPENAI_WEBSITE_RESEARCH_URL", "https://ristoh.co.ke/")
    model = os.getenv(
        "OPENAI_WEBSITE_RESEARCH_MODEL",
        settings.llm_model,
    )
    client = AsyncOpenAI(api_key=api_key)
    website_researcher = LoggingCachedWebsiteResearcher(
        create_platform_website_researcher(settings)
    )
    logger.info(
        "Live website research configuration provider=%s researcher=%s "
        "project_id=%s max_results=%s timeout_seconds=%s fetch_timeout_seconds=%s",
        settings.platform_web_search_provider,
        type(website_researcher.delegate).__name__,
        settings.platform_web_search_project_id,
        settings.platform_web_search_max_results,
        settings.platform_web_search_timeout_seconds,
        settings.onboarding_website_fetch_timeout_seconds,
    )
    research_result = await website_researcher.research(website_url)
    if provider == "tavily":
        assert research_result.sources, (
            "Expected Tavily to return at least one website research source for "
            f"{website_url} using project_id={settings.platform_web_search_project_id}.\n\n"
            f"Research response:\n{research_result.model_dump_json(indent=2)}"
        )

    analyzer = OpenAIWebsiteAnalyzer(
        client.responses,
        model=model,
        website_researcher=website_researcher,
        fetch_timeout_seconds=settings.onboarding_website_fetch_timeout_seconds,
    )

    analysis = await analyzer.analyze(
        OnboardingSessionRecord(
            website_url=website_url,
            admin=OnboardingAdmin(
                name="Live Test Admin",
                email="admin@example.com",
                phone_number="+254110101010",
                role_title="Owner",
                authority_confirmed=True,
                terms_accepted=True,
            ),
        )
    )
    output = analysis.model_dump_json(indent=2)
    logger.info(
        "Live hybrid website analysis result website_url=%s model=%s chars=%s\n%s",
        website_url,
        model,
        len(output),
        output,
    )
    normalized_output = output.lower()
    missing = [
        fragment
        for fragment in expected_contact_fragments()
        if fragment not in normalized_output
    ]

    assert analysis.business_profile.website_url
    assert analysis.contact_info, f"Expected contact_info in analysis:\n{output}"
    assert not missing, (
        "Hybrid website analysis did not include expected contact URL fragment(s): "
        f"{missing}\n\nResponse:\n{output}"
    )
