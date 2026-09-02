import asyncio
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document

from app.adapters.memory import MemoryRetrievalStore
from app.adapters.telegram import TelegramBotInfo
from app.config import get_settings
from app.knowledge import SEED_KNOWLEDGE_NAMESPACE, stable_source_hash
from app.main import app
from app.models import (
    OnboardingBusinessProfile,
    OnboardingContactPoint,
    WebsiteAnalysisResult,
    WebsiteResearchSource,
)
from app.provider_projects import MetadataOnlyProviderProjectProvisioner


def validation_messages(response) -> list[str]:
    return [str(error.get("msg", "")) for error in response.json()["detail"]]


def seed_default_knowledge(client: TestClient, filename: str, content: str) -> None:
    source = f"kb/{filename}"
    asyncio.run(
        client.app.state.container.retrieval.upsert(
            [
                Document(
                    page_content=content,
                    metadata={
                        "source": source,
                        "chunk_id": f"{source}#0000",
                    },
                )
            ],
            SEED_KNOWLEDGE_NAMESPACE,
        )
    )


class FakeTelegramSender:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str, str]] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        tenant_id: str = "default",
    ) -> None:
        self.sent_messages.append((chat_id, text, tenant_id))


class FakeWhatsAppSender:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str, str]] = []

    async def send_message(
        self,
        to: str,
        text: str,
        tenant_id: str = "default",
    ) -> None:
        self.sent_messages.append((to, text, tenant_id))


class FakeTelegramCredentials:
    def __init__(self, webhook_secret_token: str | None = None) -> None:
        self.bot_token = "fake-token"
        self.webhook_secret_token = webhook_secret_token


class FakeTelegramCredentialResolver:
    def __init__(self, webhook_secret_token: str | None = None) -> None:
        self.webhook_secret_token = webhook_secret_token
        self.tenant_ids: list[str] = []

    async def resolve(self, tenant_id: str) -> FakeTelegramCredentials:
        self.tenant_ids.append(tenant_id)
        return FakeTelegramCredentials(self.webhook_secret_token)


class FakeTelegramWebhookRegistrar:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.registrations: list[dict[str, str]] = []

    async def register_webhook(
        self,
        *,
        tenant_id: str,
        bot_token: str,
        webhook_secret_token: str,
    ) -> str | None:
        if self.fail:
            raise RuntimeError("Telegram webhook registration failed")
        self.registrations.append(
            {
                "tenant_id": tenant_id,
                "bot_token": bot_token,
                "webhook_secret_token": webhook_secret_token,
            }
        )
        return f"https://example.com/webhooks/telegram?tenant_id={tenant_id}"


class FakeTelegramBotInfoResolver:
    def __init__(self, username: str = "hustle_hq_bot") -> None:
        self.username = username
        self.bot_tokens: list[str] = []

    async def get_bot_info(self, *, bot_token: str) -> TelegramBotInfo:
        self.bot_tokens.append(bot_token)
        return TelegramBotInfo(username=self.username, first_name="Hustle HQ Bot")


class FakeTelegramSecretWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.secrets: list[dict[str, str]] = []

    async def write_secret(
        self,
        *,
        secret_name: str,
        bot_token: str,
        webhook_secret_token: str,
    ) -> None:
        if self.fail:
            raise RuntimeError("Telegram Secret creation failed")
        self.secrets.append(
            {
                "secret_name": secret_name,
                "bot_token": bot_token,
                "webhook_secret_token": webhook_secret_token,
            }
        )


class FakeWhatsAppCredentials:
    def __init__(self, verify_token: str | None = None) -> None:
        self.access_token = "fake-token"
        self.phone_number_id = "fake-phone-number-id"
        self.verify_token = verify_token
        self.graph_api_version = "v20.0"


class FakeWhatsAppCredentialResolver:
    def __init__(self, verify_token: str | None = None) -> None:
        self.verify_token = verify_token
        self.tenant_ids: list[str] = []

    async def resolve(self, tenant_id: str) -> FakeWhatsAppCredentials:
        self.tenant_ids.append(tenant_id)
        return FakeWhatsAppCredentials(self.verify_token)


class FakeWebsiteAnalyzer:
    async def analyze(self, session) -> WebsiteAnalysisResult:
        return WebsiteAnalysisResult(
            business_profile=OnboardingBusinessProfile(
                business_name="Hustle HQ",
                website_url=session.website_url,
                location_name="Hustle HQ",
                physical_location="Enterprise Road",
                business_phone="+254110101010",
                business_email="hello@hustlehq.example",
            ),
            business_summary=(
                "Represent Hustle HQ and answer from approved business facts."
            ),
            contact_info=[
                OnboardingContactPoint(
                    kind="website",
                    label="Website",
                    url=session.website_url,
                    is_primary=True,
                )
            ],
            knowledge_sources=[
                WebsiteResearchSource(
                    url="https://hustlehq.example/about",
                    title="About Hustle HQ",
                    text="Hustle HQ serves customers from Enterprise Road.",
                    provider="tavily",
                )
            ],
        )


class CountingWebsiteAnalyzer(FakeWebsiteAnalyzer):
    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, session) -> WebsiteAnalysisResult:
        self.calls += 1
        return await super().analyze(session)


class FailingWebsiteAnalyzer:
    async def analyze(self, session) -> WebsiteAnalysisResult:
        raise ValueError("sensitive provider failure details")


class FailingRetrievalStore:
    async def initialize(self) -> None:
        return None

    async def upsert(self, documents: list[Document], namespace: str) -> None:
        raise RuntimeError("KB failure")

    async def delete_by_metadata(
        self,
        namespace: str,
        metadata_key: str,
        metadata_value: str,
    ) -> None:
        return None

    async def delete_by_source_url(self, namespace: str, source_url: str) -> None:
        return None

    async def search(
        self,
        query: str,
        namespace: str,
        limit: int = 4,
    ) -> list[Document]:
        return []


class FailOnceAfterUpsertRetrievalStore(MemoryRetrievalStore):
    def __init__(self) -> None:
        super().__init__()
        self.should_fail = True

    async def upsert(self, documents: list[Document], namespace: str) -> None:
        await super().upsert(documents, namespace)
        if self.should_fail:
            self.should_fail = False
            raise RuntimeError("KB partial failure")


class FailingProviderProjectProvisioner:
    async def provision_for(self, **kwargs):
        raise RuntimeError("Provider project provisioning failed")


@pytest.fixture(autouse=True)
def clear_settings_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.container.create_openai_website_analyzer",
        lambda **_kwargs: FakeWebsiteAnalyzer(),
    )
    monkeypatch.setattr(
        "app.container.TelegramBotApiInfoResolver",
        lambda: FakeTelegramBotInfoResolver(),
    )
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_synthetic_webhook_vertical_slice() -> None:
    with TestClient(app) as client:
        seed_default_knowledge(
            client,
            "refunds.txt",
            "Refund requests can be submitted within 30 days of purchase.",
        )
        response = client.post(
            "/webhooks/synthetic",
            json={
                "event_id": "api-event-1",
                "external_chat_id": "chat-1",
                "external_user_id": "user-1",
                "text": "What is the refund policy?",
            },
        )

    assert response.status_code == 200
    assert response.json()["citations"] == ["kb/refunds.txt#0000"]


def test_customer_message_endpoint_receives_user_question() -> None:
    with TestClient(app) as client:
        seed_default_knowledge(
            client,
            "password-reset.txt",
            "To reset your password, open Settings, select Security, then choose Reset password.",
        )
        response = client.post(
            "/messages/customer",
            json={
                "event_id": "api-event-customer-message-1",
                "external_chat_id": "chat-customer-message-1",
                "external_user_id": "user-customer-message-1",
                "text": "How do I reset my password?",
            },
        )

    assert response.status_code == 200
    assert response.json()["citations"] == ["kb/password-reset.txt#0000"]
    assert response.json()["state"] == "BOT_ACTIVE"


def test_customer_message_endpoint_accepts_tenant_header() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/messages/customer",
            headers={"X-Agent-Tenant-Id": "tenant-a"},
            json={
                "event_id": "api-tenant-event-1",
                "external_chat_id": "api-tenant-chat-1",
                "external_user_id": "api-tenant-user-1",
                "text": "Unknown question?",
            },
        )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"


def test_tenant_can_be_created_with_generated_id_without_config() -> None:
    with TestClient(app) as client:
        create_response = client.post(
            "/tenants",
            json={
                "display_name": "Hustle HQ",
                "selected_plan": "enterprise",
            },
        )
        tenant_id = create_response.json()["tenant_id"]
        get_response = client.get(f"/tenants/{tenant_id}")
        config_response = client.get(f"/tenants/{tenant_id}/config")

    assert create_response.status_code == 201
    assert tenant_id.startswith("tnt_")
    assert "config" not in create_response.json()
    assert create_response.json()["slug"] == "hustle-hq"
    assert create_response.json()["display_name"] == "Hustle HQ"
    assert create_response.json()["selected_plan"] == "enterprise"
    assert get_response.status_code == 200
    assert get_response.json()["tenant_id"] == tenant_id
    assert config_response.status_code == 404
    assert config_response.json()["detail"] == "Tenant config not found"


def test_tenant_creation_returns_conflict_when_slug_already_exists() -> None:
    with TestClient(app) as client:
        first_response = client.post("/tenants", json={"display_name": "Hustle HQ"})
        second_response = client.post("/tenants", json={"display_name": "Hustle HQ"})

    assert first_response.status_code == 201
    assert second_response.status_code == 409
    assert first_response.json()["slug"] == "hustle-hq"
    assert (
        second_response.json()["detail"]
        == "Tenant with matching name or slug already exists"
    )


def test_tenant_can_be_read_by_slug() -> None:
    with TestClient(app) as client:
        create_response = client.post("/tenants", json={"display_name": "Slug Lookup Ltd"})
        tenant_id = create_response.json()["tenant_id"]
        lookup_response = client.get("/tenants/by-slug/slug-lookup-ltd")

    assert create_response.status_code == 201
    assert lookup_response.status_code == 200
    assert lookup_response.json()["tenant_id"] == tenant_id
    assert lookup_response.json()["slug"] == "slug-lookup-ltd"
    assert "config" not in lookup_response.json()


def onboarding_job_payload() -> dict:
    return {
        "idempotency_key": "onboarding-hustle-hq-001",
        "callback_url": "https://n8n.example/webhook/onboarding-result",
        "selected_plan": "sme",
        "admin": {
            "username_email": "admin@hustlehq.example",
            "given_name": "John",
            "family_name": "Doe",
            "phone_number": "+254110101010",
            "role_title": "Owner",
            "authority_confirmed": True,
            "terms_accepted": True,
        },
        "business_profile": {
            "business_name": "Hustle HQ Onboarding",
            "website_url": "https://hustlehq.example",
            "location_name": "Hustle HQ",
            "physical_location": "Enterprise Road",
            "business_phone": "+254110101010",
            "business_email": "hello@hustlehq.example",
            "google_place_url": "https://maps.google.com/?q=Hustle+HQ",
        },
        "business_summary": "Represent Hustle HQ and answer from business facts.",
        "contact_info": [
            {
                "kind": "instagram",
                "label": "Instagram",
                "url": "https://instagram.com/hustlehq",
            }
        ],
        "telegram": {
            "bot_token": "123456:telegram-token",
            "webhook_secret_token": "telegram-webhook-secret",
        },
        "provider_projects": {
            "llm_project_id": "proj_hustle",
            "llm_project_name": "customer-service-hustle-hq",
            "langsmith_project": "customer-service-hustle-hq",
        },
        "knowledge_sources": [
            {
                "url": "https://hustlehq.example/about",
                "title": "About Hustle HQ",
                "text": "Hustle HQ serves customers from Enterprise Road.",
                "provider": "tavily",
                "retrieved_at": "2026-08-26T10:00:00Z",
            }
        ],
    }


def onboarding_job_retry_payload(bot_token: str = "123456:telegram-retry-token") -> dict:
    return {
        "telegram": {
            "bot_token": bot_token,
        }
    }


def onboarding_session_payload() -> dict:
    return {
        "admin": {
            "username_email": "admin@hustlehq.example",
            "given_name": "John",
            "family_name": "Doe",
            "phone_number": "+254110101010",
            "role_title": "Owner",
            "authority_confirmed": True,
            "terms_accepted": True,
        },
    }


def onboarding_website_payload(
    website_url: str = "https://hustlehq.example",
    website_verification_email: str = "admin@hustlehq.example",
) -> dict:
    return {
        "website_url": website_url,
        "website_verification_email": website_verification_email,
    }


def create_reviewed_onboarding_session(client: TestClient) -> dict:
    return create_reviewed_onboarding_session_with_options(
        client,
        include_provider_projects=True,
    )


def create_reviewed_onboarding_session_with_options(
    client: TestClient,
    *,
    include_provider_projects: bool,
) -> dict:
    create_response = client.post(
        "/onboarding/sessions",
        json=onboarding_session_payload(),
    )
    session_id = create_response.json()["session_id"]
    verify_onboarding_username_email(client, session_id)
    save_response = client.patch(
        f"/onboarding/sessions/{session_id}/website",
        json=onboarding_website_payload(),
    )
    assert save_response.status_code == 200
    verify_onboarding_website_email(client, session_id)
    analyze_response = client.post(
        f"/onboarding/sessions/{session_id}/analyze-website",
    )
    reviewed = analyze_response.json()
    update_payload = {
        "business_profile": {
            **reviewed["business_profile"],
            "business_name": "Hustle HQ Session",
            "location_name": "Hustle HQ",
            "physical_location": "Enterprise Road",
            "business_phone": "+254110101010",
            "business_email": "hello@hustlehq.example",
        },
        "business_summary": (
            "Represent Hustle HQ and answer from approved facts."
        ),
        "contact_info": [
            {
                "kind": "instagram",
                "label": "Instagram",
                "url": "https://instagram.com/hustlehq",
            }
        ],
    }
    if include_provider_projects:
        update_payload["provider_projects"] = {
            "llm_project_name": "customer-service-hustle-hq-session",
            "langsmith_project": "customer-service-hustle-hq-session",
        }
    update_response = client.patch(
        f"/onboarding/sessions/{session_id}",
        json=update_payload,
    )
    assert update_response.status_code == 200
    return update_response.json()


def token_from_setup_url(setup_url: str) -> str:
    parsed = urlparse(setup_url)
    return parse_qs(parsed.query)["token"][0]


def token_from_latest_email(client: TestClient) -> tuple[str, str]:
    email_sender = client.app.state.container.email_sender
    latest = email_sender.sent_messages[-1]
    match = re.search(r"/verify-[^?\s]+\?session_id=([^&\s]+)&token=([^\s]+)", latest.text)
    assert match
    return match.group(1), match.group(2)


def verify_onboarding_username_email(client: TestClient, session_id: str) -> dict:
    link_session_id, token = token_from_latest_email(client)
    assert link_session_id == session_id
    response = client.post(
        f"/onboarding/sessions/{session_id}/verify-username-email",
        json={"token": token},
    )
    assert response.status_code == 200
    return response.json()


def verify_onboarding_website_email(client: TestClient, session_id: str) -> dict:
    link_session_id, token = token_from_latest_email(client)
    assert link_session_id == session_id
    response = client.post(
        f"/onboarding/sessions/{session_id}/verify-website-email",
        json={"token": token},
    )
    assert response.status_code == 200
    return response.json()


def test_onboarding_session_accepts_valid_start_fields() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/onboarding/sessions",
            json=onboarding_session_payload(),
        )
        sent_email = client.app.state.container.email_sender.sent_messages[-1]

    assert response.status_code == 201
    assert response.json()["status"] == "username_email_verification_pending"
    assert response.json()["current_step"] == "username-email-verification"
    assert response.json()["username_email_verified"] is False
    assert response.json()["website_email_verified"] is False
    assert response.json()["admin"]["username_email"] == "admin@hustlehq.example"
    assert response.json()["admin"]["given_name"] == "John"
    assert response.json()["admin"]["family_name"] == "Doe"
    assert response.json()["terms_version"] == "beta-2026-08-28"
    assert response.json()["terms_accepted_at"]
    assert sent_email.to == ["admin@hustlehq.example"]
    assert (
        f"http://localhost:5173?session_id={response.json()['session_id']}"
        in sent_email.text
    )


def test_onboarding_session_blocks_analysis_until_both_emails_are_verified() -> None:
    with TestClient(app) as client:
        create_response = client.post(
            "/onboarding/sessions",
            json=onboarding_session_payload(),
        )
        session_id = create_response.json()["session_id"]
        blocked_response = client.post(
            f"/onboarding/sessions/{session_id}/analyze-website",
        )
        username_verified = verify_onboarding_username_email(client, session_id)
        still_blocked_response = client.post(
            f"/onboarding/sessions/{session_id}/analyze-website",
        )
        save_response = client.patch(
            f"/onboarding/sessions/{session_id}/website",
            json=onboarding_website_payload(),
        )
        website_verified = verify_onboarding_website_email(client, session_id)
        analyze_response = client.post(
            f"/onboarding/sessions/{session_id}/analyze-website",
        )

    assert blocked_response.status_code == 409
    assert blocked_response.json()["detail"] == (
        "Username and website emails must be verified before analysis"
    )
    assert username_verified["username_email_verified"] is True
    assert username_verified["status"] == "draft"
    assert username_verified["current_step"] == "website"
    assert still_blocked_response.status_code == 409
    assert save_response.status_code == 200
    assert website_verified["website_email_verified"] is True
    assert website_verified["current_step"] == "analyzing"
    assert analyze_response.status_code == 200
    assert analyze_response.json()["status"] == "ready_for_review"


def test_onboarding_analysis_is_idempotent_after_success() -> None:
    analyzer = CountingWebsiteAnalyzer()
    with TestClient(app) as client:
        client.app.state.container.onboarding_sessions.website_analyzer = analyzer
        create_response = client.post(
            "/onboarding/sessions",
            json=onboarding_session_payload(),
        )
        session_id = create_response.json()["session_id"]
        verify_onboarding_username_email(client, session_id)
        save_response = client.patch(
            f"/onboarding/sessions/{session_id}/website",
            json=onboarding_website_payload(),
        )
        assert save_response.status_code == 200
        verify_onboarding_website_email(client, session_id)

        first_response = client.post(
            f"/onboarding/sessions/{session_id}/analyze-website",
        )
        second_response = client.post(
            f"/onboarding/sessions/{session_id}/analyze-website",
        )
        forced_response = client.post(
            f"/onboarding/sessions/{session_id}/analyze-website?force=true",
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert forced_response.status_code == 200
    assert first_response.json()["analysis"] == second_response.json()["analysis"]
    assert analyzer.calls == 2


def test_onboarding_analysis_internal_errors_are_generic() -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        create_response = client.post(
            "/onboarding/sessions",
            json=onboarding_session_payload(),
        )
        session_id = create_response.json()["session_id"]
        verify_onboarding_username_email(client, session_id)
        save_response = client.patch(
            f"/onboarding/sessions/{session_id}/website",
            json=onboarding_website_payload(),
        )
        assert save_response.status_code == 200
        verify_onboarding_website_email(client, session_id)
        client.app.state.container.onboarding_sessions.website_analyzer = (
            FailingWebsiteAnalyzer()
        )

        response = client.post(f"/onboarding/sessions/{session_id}/analyze-website")

    body = response.json()
    assert response.status_code == 500
    assert body["detail"].startswith("An internal error has occurred. Reference: ERR-")
    assert body["error_id"].startswith("ERR-")
    assert "trace_id" not in body
    assert "span_id" not in body
    assert "x-trace-id" not in response.headers
    assert "x-span-id" not in response.headers
    assert response.headers["x-error-id"] == body["error_id"]
    assert "sensitive provider failure details" not in body["detail"]


def test_onboarding_session_rejects_used_username_email_verification_token() -> None:
    with TestClient(app) as client:
        create_response = client.post(
            "/onboarding/sessions",
            json=onboarding_session_payload(),
        )
        session_id = create_response.json()["session_id"]
        _link_session_id, token = token_from_latest_email(client)
        first_response = client.post(
            f"/onboarding/sessions/{session_id}/verify-username-email",
            json={"token": token},
        )
        second_response = client.post(
            f"/onboarding/sessions/{session_id}/verify-username-email",
            json={"token": token},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 422
    assert second_response.json()["detail"] == (
        "Email verification link is missing, expired, invalid, or already used"
    )


def test_onboarding_session_rejects_invalid_website_url() -> None:
    with TestClient(app) as client:
        create_response = client.post("/onboarding/sessions", json=onboarding_session_payload())
        session_id = create_response.json()["session_id"]
        verify_onboarding_username_email(client, session_id)
        response = client.patch(
            f"/onboarding/sessions/{session_id}/website",
            json=onboarding_website_payload(website_url="not-a-url"),
        )

    assert response.status_code == 422
    assert any("valid URL" in message for message in validation_messages(response))


def test_onboarding_session_rejects_non_http_website_url() -> None:
    with TestClient(app) as client:
        create_response = client.post("/onboarding/sessions", json=onboarding_session_payload())
        session_id = create_response.json()["session_id"]
        verify_onboarding_username_email(client, session_id)
        response = client.patch(
            f"/onboarding/sessions/{session_id}/website",
            json=onboarding_website_payload(website_url="ftp://hustlehq.example"),
        )

    assert response.status_code == 422
    assert any(
        "URL scheme should be 'http' or 'https'" in message
        for message in validation_messages(response)
    )


def test_onboarding_session_rejects_invalid_admin_email() -> None:
    payload = onboarding_session_payload()
    payload["admin"]["username_email"] = "not-an-email"

    with TestClient(app) as client:
        response = client.post("/onboarding/sessions", json=payload)

    assert response.status_code == 422
    assert any("valid email address" in message for message in validation_messages(response))


def test_onboarding_session_requires_given_and_family_names() -> None:
    payload = onboarding_session_payload()
    payload["admin"]["given_name"] = ""
    payload["admin"]["family_name"] = ""

    with TestClient(app) as client:
        response = client.post("/onboarding/sessions", json=payload)

    assert response.status_code == 422
    messages = validation_messages(response)
    assert any(
        "String should have at least 1 character" in message
        for message in messages
    )


def test_onboarding_session_rejects_invalid_admin_phone() -> None:
    payload = onboarding_session_payload()
    payload["admin"]["phone_number"] = "0723921716"

    with TestClient(app) as client:
        response = client.post("/onboarding/sessions", json=payload)

    assert response.status_code == 422
    assert any(
        "phone_number must be in international format" in message
        for message in validation_messages(response)
    )


def test_onboarding_session_requires_authority_and_terms() -> None:
    payload = onboarding_session_payload()
    payload["admin"]["authority_confirmed"] = False
    payload["admin"]["terms_accepted"] = False

    with TestClient(app) as client:
        response = client.post("/onboarding/sessions", json=payload)

    assert response.status_code == 422
    messages = validation_messages(response)
    assert any("authority_confirmed must be accepted" in message for message in messages)
    assert any("terms_accepted must be accepted" in message for message in messages)


def test_onboarding_session_rejects_missing_authority_and_terms() -> None:
    payload = onboarding_session_payload()
    payload["admin"].pop("authority_confirmed")
    payload["admin"].pop("terms_accepted")

    with TestClient(app) as client:
        response = client.post("/onboarding/sessions", json=payload)

    assert response.status_code == 422
    messages = validation_messages(response)
    assert messages.count("Field required") == 2


def test_onboarding_session_rejects_mismatched_admin_email_domain() -> None:
    with TestClient(app) as client:
        create_response = client.post("/onboarding/sessions", json=onboarding_session_payload())
        session_id = create_response.json()["session_id"]
        verify_onboarding_username_email(client, session_id)
        response = client.patch(
            f"/onboarding/sessions/{session_id}/website",
            json=onboarding_website_payload(
                website_verification_email="admin@example.net",
            ),
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "website verification email domain must belong to the website domain"
    )


def test_onboarding_session_accepts_mismatched_admin_email_domain_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_ONBOARDING_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH", "false")
    get_settings.cache_clear()

    with TestClient(app) as client:
        create_response = client.post("/onboarding/sessions", json=onboarding_session_payload())
        session_id = create_response.json()["session_id"]
        verify_onboarding_username_email(client, session_id)
        response = client.patch(
            f"/onboarding/sessions/{session_id}/website",
            json=onboarding_website_payload(
                website_verification_email="admin@example.net",
            ),
        )

    assert response.status_code == 200


def test_onboarding_session_rejects_duplicate_active_website_onboarding() -> None:
    with TestClient(app) as client:
        first_response = client.post(
            "/onboarding/sessions",
            json=onboarding_session_payload(),
        )
        first_session_id = first_response.json()["session_id"]
        verify_onboarding_username_email(client, first_session_id)
        first_website = client.patch(
            f"/onboarding/sessions/{first_session_id}/website",
            json=onboarding_website_payload(),
        )
        second_response = client.post(
            "/onboarding/sessions",
            json={
                "admin": {
                    **onboarding_session_payload()["admin"],
                    "username_email": "other@hustlehq.example",
                }
            },
        )
        second_session_id = second_response.json()["session_id"]
        verify_onboarding_username_email(client, second_session_id)
        duplicate_response = client.patch(
            f"/onboarding/sessions/{second_session_id}/website",
            json=onboarding_website_payload(
                website_url="https://www.hustlehq.example/contact",
            ),
        )

    assert first_response.status_code == 201
    assert first_website.status_code == 200
    assert second_response.status_code == 201
    assert duplicate_response.status_code == 422
    assert duplicate_response.json()["detail"] == (
        "An active onboarding session for this website already exists; "
        "use the resume link or contact support"
    )


def test_onboarding_session_rejects_existing_tenant_website() -> None:
    with TestClient(app) as client:
        job_response = client.post("/admin/onboarding/jobs", json=onboarding_job_payload())
        create_response = client.post(
            "/onboarding/sessions",
            json=onboarding_session_payload(),
        )
        session_id = create_response.json()["session_id"]
        verify_onboarding_username_email(client, session_id)
        response = client.patch(
            f"/onboarding/sessions/{session_id}/website",
            json=onboarding_website_payload(),
        )

    assert job_response.status_code == 202
    assert response.status_code == 422
    assert response.json()["detail"] == "A tenant with this website already exists"


def test_onboarding_session_can_save_and_reload_wizard_data() -> None:
    with TestClient(app) as client:
        session = create_reviewed_onboarding_session(client)
        response = client.get(f"/onboarding/sessions/{session['session_id']}")

    assert response.status_code == 200
    assert response.json()["business_profile"]["business_name"] == "Hustle HQ Session"
    assert response.json()["business_summary"] == (
        "Represent Hustle HQ and answer from approved facts."
    )
    assert response.json()["contact_info"][0]["kind"] == "instagram"


def test_onboarding_session_rejects_missing_or_invalid_telegram_token() -> None:
    with TestClient(app) as client:
        session = create_reviewed_onboarding_session(client)
        response = client.post(
            f"/onboarding/sessions/{session['session_id']}/telegram-setup",
            json={
                "token": "wrong-token",
                "bot_token": "123456:telegram-token",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Telegram setup link is missing, expired, invalid, or already used"
    )


def test_onboarding_session_rejects_telegram_secret_name() -> None:
    with TestClient(app) as client:
        session = create_reviewed_onboarding_session(client)
        setup_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/request-telegram-setup",
        )
        token = token_from_setup_url(setup_response.json()["telegram_setup_url"])
        response = client.post(
            f"/onboarding/sessions/{session['session_id']}/telegram-setup",
            json={
                "token": token,
                "bot_token": "123456:telegram-token",
                "secret_name": "tenant-hustle-hq-telegram",
            },
        )

    assert response.status_code == 422
    assert "Extra inputs are not permitted" in response.text


def test_onboarding_session_rejects_telegram_webhook_secret_token() -> None:
    with TestClient(app) as client:
        session = create_reviewed_onboarding_session(client)
        setup_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/request-telegram-setup",
        )
        token = token_from_setup_url(setup_response.json()["telegram_setup_url"])
        response = client.post(
            f"/onboarding/sessions/{session['session_id']}/telegram-setup",
            json={
                "token": token,
                "bot_token": "123456:telegram-token",
                "webhook_secret_token": "telegram-secret",
            },
        )

    assert response.status_code == 422
    assert "Extra inputs are not permitted" in response.text


def test_onboarding_job_rejects_telegram_secret_name() -> None:
    payload = onboarding_job_payload()
    payload["telegram"]["secret_name"] = "tenant-hustle-hq-telegram"

    with TestClient(app) as client:
        response = client.post("/admin/onboarding/jobs", json=payload)

    assert response.status_code == 422
    assert "Extra inputs are not permitted" in response.text


def test_onboarding_session_emails_saas_admin_for_telegram_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AGENT_ONBOARDING_REVIEW_EMAIL",
        "onboarding-review@example.com",
    )

    with TestClient(app) as client:
        session = create_reviewed_onboarding_session(client)
        response = client.post(
            f"/onboarding/sessions/{session['session_id']}/request-telegram-setup",
        )
        sent_email = client.app.state.container.email_sender.sent_messages[-1]

    assert response.status_code == 200
    assert sent_email.to == ["onboarding-review@example.com"]
    assert sent_email.subject == "Telegram setup needed for Hustle HQ Session"
    assert response.json()["telegram_setup_url"] in sent_email.text
    assert "Business: Hustle HQ Session" in sent_email.text
    assert "admin@hustlehq.example" not in sent_email.text
    assert "Enterprise Road" not in sent_email.text
    assert "https://instagram.com/hustlehq" not in sent_email.text


def test_onboarding_session_rejects_used_telegram_token() -> None:
    with TestClient(app) as client:
        session = create_reviewed_onboarding_session(client)
        setup_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/request-telegram-setup",
        )
        token = token_from_setup_url(setup_response.json()["telegram_setup_url"])
        first_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/telegram-setup",
            json={
                "token": token,
                "bot_token": "123456:telegram-token",
            },
        )
        second_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/telegram-setup",
            json={
                "token": token,
                "bot_token": "123456:telegram-token",
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 422
    assert second_response.json()["detail"] == (
        "Telegram setup link is missing, expired, invalid, or already used"
    )


def test_onboarding_session_submits_completed_session_into_job_flow() -> None:
    with TestClient(app) as client:
        session = create_reviewed_onboarding_session(client)
        setup_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/request-telegram-setup",
        )
        token = token_from_setup_url(setup_response.json()["telegram_setup_url"])
        telegram_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/telegram-setup",
            json={
                "token": token,
                "bot_token": "123456:telegram-token",
            },
        )
        submit_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/submit",
        )
        job_response = client.get(
            f"/admin/onboarding/jobs/{submit_response.json()['job_id']}",
        )
        sent_email = client.app.state.container.email_sender.sent_messages[-1]

    assert telegram_response.status_code == 200
    generated_webhook_secret = telegram_response.json()["telegram"][
        "webhook_secret_token"
    ]
    assert re.fullmatch(r"[0-9a-f]{64}", generated_webhook_secret)
    assert submit_response.status_code == 202
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "succeeded"
    assert job_response.json()["tenant_slug"] == "hustle-hq-session"
    assert sent_email.subject == "Customer-service onboarding completed"
    assert "admin@hustlehq.example" in sent_email.to
    assert "https://t.me/hustle_hq_bot" in sent_email.text
    assert "Tenant ID:" not in sent_email.text
    assert "Tenant slug:" not in sent_email.text


def test_onboarding_job_success_email_sends_bot_link_to_tenant_and_saas_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AGENT_ONBOARDING_REVIEW_EMAIL",
        "onboarding-review@example.com",
    )
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-success-email-bot-link"
    payload["business_profile"]["business_name"] = "Hustle HQ Success Email"

    with TestClient(app) as client:
        bot_info_resolver = FakeTelegramBotInfoResolver(username="hustle_hq_success_bot")
        client.app.state.container.onboarding_jobs.telegram_bot_info_resolver = (
            bot_info_resolver
        )
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")
        sent_messages = client.app.state.container.email_sender.sent_messages

    assert response.status_code == 202
    assert job_response.json()["status"] == "succeeded"
    assert bot_info_resolver.bot_tokens == ["123456:telegram-token"]
    completion_emails = [
        email
        for email in sent_messages
        if email.subject == "Customer-service onboarding completed"
    ]
    assert len(completion_emails) == 2
    tenant_admin_email = next(
        email for email in completion_emails if email.to == ["admin@hustlehq.example"]
    )
    saas_admin_email = next(
        email for email in completion_emails if email.to == ["onboarding-review@example.com"]
    )
    assert "https://t.me/hustle_hq_success_bot" in tenant_admin_email.text
    assert "Tenant ID:" not in tenant_admin_email.text
    assert "Tenant slug:" not in tenant_admin_email.text
    assert "https://t.me/hustle_hq_success_bot" in saas_admin_email.text
    assert f"Tenant ID: {job_response.json()['tenant_id']}" in saas_admin_email.text
    assert "Tenant slug: hustle-hq-success-email" in saas_admin_email.text


def test_telegram_setup_submits_session_and_job_provisions_provider_projects() -> None:
    with TestClient(app) as client:
        session = create_reviewed_onboarding_session_with_options(
            client,
            include_provider_projects=False,
        )
        setup_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/request-telegram-setup",
        )
        token = token_from_setup_url(setup_response.json()["telegram_setup_url"])
        telegram_response = client.post(
            f"/onboarding/sessions/{session['session_id']}/telegram-setup",
            json={
                "token": token,
                "bot_token": "123456:telegram-token",
            },
        )
        submitted_session = telegram_response.json()
        job_id = submitted_session["submitted_job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")
        tenant_id = job_response.json()["tenant_id"]
        config_response = client.get(f"/tenants/{tenant_id}/config")

    assert telegram_response.status_code == 200
    assert submitted_session["status"] == "submitted"
    assert submitted_session["current_step"] == "complete"
    assert submitted_session["provider_projects"]["llm_project_id"] is None
    assert submitted_session["provider_projects"]["llm_project_name"] is None
    assert submitted_session["provider_projects"]["langsmith_project"] is None
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "succeeded"
    assert config_response.status_code == 200
    assert config_response.json()["llm_project_name"] == (
        "customer-service-hustle-hq-session"
    )
    assert config_response.json()["langsmith_project"] == (
        "customer-service-hustle-hq-session"
    )
    assert config_response.json()["telegram_secret_name"] == (
        "tenant-hustle-hq-session-telegram"
    )


def test_onboarding_job_accepts_and_provisions_internal_records() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/admin/onboarding/jobs",
            json=onboarding_job_payload(),
        )
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")
        tenant_id = job_response.json()["tenant_id"]
        config_response = client.get(f"/tenants/{tenant_id}/config")

        onboarding_repo = client.app.state.container.onboarding
        business_profile = onboarding_repo.business_profiles[tenant_id]
        contact_points = onboarding_repo.contact_points[tenant_id]
        membership = onboarding_repo.memberships[(tenant_id, "admin@hustlehq.example")]
        namespace = config_response.json()["vector_namespace"]
        kb_documents = client.app.state.container.retrieval.documents[namespace]

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "succeeded"
    assert job_response.json()["tenant_slug"] == "hustle-hq-onboarding"
    assert config_response.status_code == 200
    assert config_response.json()["business_summary"] == (
        "Represent Hustle HQ and answer from business facts."
    )
    assert config_response.json()["telegram_secret_name"] == (
        "tenant-hustle-hq-onboarding-telegram"
    )
    assert config_response.json()["web_search_provider"] == "tavily"
    assert config_response.json()["web_search_project_name"] == "hustle-hq-onboarding"
    assert business_profile.business_name == "Hustle HQ Onboarding"
    assert [point.kind for point in contact_points] == [
        "phone",
        "email",
        "website",
        "map",
        "instagram",
    ]
    assert membership.role == "owner"
    assert any(
        document.metadata["source"] == "onboarding:approved-profile"
        and document.metadata["source_url"] == "https://hustlehq.example/"
        and document.metadata["source_type"] == "onboarding"
        and document.metadata["source_title"] == "Hustle HQ Onboarding"
        and document.metadata["chunk_id"].startswith(f"onboarding-profile:{tenant_id}#")
        and document.metadata["section_title"] == ""
        and "content_hash" in document.metadata
        for document in kb_documents
    )
    assert any(
        document.metadata["source"] == "website:https://hustlehq.example/about"
        and document.metadata["source_url"] == "https://hustlehq.example/about"
        and document.metadata["provider"] == "tavily"
        and document.metadata["source_type"] == "website"
        and document.metadata["source_title"] == "About Hustle HQ"
        and document.metadata["chunk_id"].startswith(
            f"url:{stable_source_hash('https://hustlehq.example/about')}#"
        )
        and document.metadata["chunk_index"] == 0
        and document.metadata["chunk_count"] >= 1
        and "content_hash" in document.metadata
        for document in kb_documents
    )


async def test_memory_retrieval_can_refresh_url_backed_chunks() -> None:
    retrieval = MemoryRetrievalStore()
    namespace = "tenant-a"
    old_url = "https://hustlehq.example/about"
    other_url = "https://hustlehq.example/menu"

    await retrieval.upsert(
        [
            Document(
                page_content="Old about page content.",
                metadata={
                    "chunk_id": "url:old#0000",
                    "source_url": old_url,
                },
            ),
            Document(
                page_content="Menu page content.",
                metadata={
                    "chunk_id": "url:menu#0000",
                    "source_url": other_url,
                },
            ),
        ],
        namespace,
    )

    await retrieval.delete_by_source_url(namespace, old_url)
    await retrieval.upsert(
        [
            Document(
                page_content="Updated about page content.",
                metadata={
                    "chunk_id": "url:new#0000",
                    "source_url": old_url,
                },
            )
        ],
        namespace,
    )

    documents = retrieval.documents[namespace]
    assert not any(document.page_content == "Old about page content." for document in documents)
    assert any(document.page_content == "Updated about page content." for document in documents)
    assert any(document.metadata["source_url"] == other_url for document in documents)


def test_onboarding_job_registers_telegram_webhook() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-telegram-webhook-001"
    payload["business_profile"]["business_name"] = "Hustle HQ Telegram Webhook"
    registrar = FakeTelegramWebhookRegistrar()

    with TestClient(app) as client:
        client.app.state.container.onboarding_jobs.telegram_webhook_registrar = registrar
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")

    assert response.status_code == 202
    assert job_response.json()["status"] == "succeeded"
    assert len(registrar.registrations) == 1
    assert registrar.registrations[0]["tenant_id"] == job_response.json()["tenant_id"]
    assert registrar.registrations[0]["bot_token"] == "123456:telegram-token"
    assert registrar.registrations[0]["webhook_secret_token"] == "telegram-webhook-secret"


def test_onboarding_job_creates_tenant_telegram_secret_before_webhook() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-telegram-secret-001"
    payload["business_profile"]["business_name"] = "Hustle HQ Telegram Secret"
    secret_writer = FakeTelegramSecretWriter()
    registrar = FakeTelegramWebhookRegistrar()

    with TestClient(app) as client:
        client.app.state.container.onboarding_jobs.telegram_secret_writer = secret_writer
        client.app.state.container.onboarding_jobs.telegram_webhook_registrar = registrar
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")
        tenant_id = job_response.json()["tenant_id"]
        config_response = client.get(f"/tenants/{tenant_id}/config")

    assert response.status_code == 202
    assert job_response.json()["status"] == "succeeded"
    assert config_response.json()["telegram_secret_name"] == (
        "tenant-hustle-hq-telegram-secret-telegram"
    )
    assert secret_writer.secrets == [
        {
            "secret_name": "tenant-hustle-hq-telegram-secret-telegram",
            "bot_token": "123456:telegram-token",
            "webhook_secret_token": "telegram-webhook-secret",
        }
    ]
    assert registrar.registrations[0]["webhook_secret_token"] == (
        secret_writer.secrets[0]["webhook_secret_token"]
    )


def test_onboarding_job_fails_when_initial_knowledge_creation_fails() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-kb-failure-001"
    payload["business_profile"]["business_name"] = "Hustle HQ KB Failure"

    with TestClient(app) as client:
        client.app.state.container.onboarding_jobs.retrieval = FailingRetrievalStore()
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")

    assert response.status_code == 202
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "failed"
    assert job_response.json()["tenant_slug"] == "hustle-hq-kb-failure"
    assert job_response.json()["error"] == "KB failure"


def test_onboarding_job_fails_when_provider_project_provisioning_fails() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-provider-failure-001"
    payload["business_profile"]["business_name"] = "Hustle HQ Provider Failure"

    with TestClient(app) as client:
        registrar = FakeTelegramWebhookRegistrar()
        client.app.state.container.onboarding_jobs.telegram_webhook_registrar = registrar
        client.app.state.container.onboarding_jobs.provider_project_provisioner = (
            FailingProviderProjectProvisioner()
        )
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")

    assert response.status_code == 202
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "failed"
    assert job_response.json()["tenant_id"] is None
    assert job_response.json()["tenant_slug"] is None
    assert job_response.json()["error"] == "Provider project provisioning failed"


def test_onboarding_job_fails_when_telegram_secret_creation_fails() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-telegram-secret-failure-001"
    payload["business_profile"]["business_name"] = "Hustle HQ Telegram Secret Failure"

    with TestClient(app) as client:
        client.app.state.container.onboarding_jobs.telegram_secret_writer = (
            FakeTelegramSecretWriter(fail=True)
        )
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")

    assert response.status_code == 202
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "failed"
    assert job_response.json()["tenant_slug"] == "hustle-hq-telegram-secret-failure"
    assert job_response.json()["error"] == "Telegram Secret creation failed"


def test_onboarding_job_fails_when_telegram_webhook_registration_fails() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-telegram-webhook-failure-001"
    payload["business_profile"]["business_name"] = "Hustle HQ Telegram Failure"

    with TestClient(app) as client:
        client.app.state.container.onboarding_jobs.telegram_webhook_registrar = (
            FakeTelegramWebhookRegistrar(fail=True)
        )
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")

    assert response.status_code == 202
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "failed"
    assert job_response.json()["tenant_slug"] == "hustle-hq-telegram-failure"
    assert job_response.json()["error"] == "Telegram webhook registration failed"


def test_failed_onboarding_kb_job_can_retry_without_duplicate_tenant() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-kb-retry-001"
    payload["business_profile"]["business_name"] = "Hustle HQ KB Retry"

    with TestClient(app) as client:
        retrieval = client.app.state.container.retrieval
        client.app.state.container.onboarding_jobs.retrieval = FailingRetrievalStore()
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        failed_job = client.get(f"/admin/onboarding/jobs/{job_id}")

        client.app.state.container.onboarding_jobs.retrieval = retrieval
        retry_response = client.post(
            f"/admin/onboarding/jobs/{job_id}/retry",
            json=onboarding_job_retry_payload(),
        )
        retried_job = client.get(f"/admin/onboarding/jobs/{job_id}")
        tenant_id = retried_job.json()["tenant_id"]
        config_response = client.get(f"/tenants/{tenant_id}/config")
        namespace = config_response.json()["vector_namespace"]
        kb_documents = retrieval.documents[namespace]

    assert response.status_code == 202
    assert failed_job.json()["status"] == "failed"
    assert retry_response.status_code == 202
    assert retried_job.json()["status"] == "succeeded"
    assert retried_job.json()["tenant_slug"] == "hustle-hq-kb-retry"
    onboarding_profile_chunks = [
        document
        for document in kb_documents
        if document.metadata["source"] == "onboarding:approved-profile"
    ]
    onboarding_profile_chunk_ids = [
        document.metadata["chunk_id"] for document in onboarding_profile_chunks
    ]
    assert onboarding_profile_chunks
    assert len(onboarding_profile_chunk_ids) == len(set(onboarding_profile_chunk_ids))


def test_retry_replaces_previous_onboarding_session_kb_documents() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = (
        "onboarding-session-00000000-0000-0000-0000-000000000123"
    )
    payload["business_profile"]["business_name"] = "Hustle HQ KB Replace"
    payload["knowledge_sources"] = [
        {
            "url": "https://hustlehq.example/old",
            "title": "Old source",
            "text": "Old onboarding website source.",
            "provider": "tavily",
            "retrieved_at": "2026-08-26T10:00:00Z",
        }
    ]
    with TestClient(app) as client:
        retrieval = FailOnceAfterUpsertRetrievalStore()
        client.app.state.container.onboarding_jobs.retrieval = retrieval
        response = client.post("/admin/onboarding/jobs", json=payload)
        job_id = response.json()["job_id"]
        failed_job = client.get(f"/admin/onboarding/jobs/{job_id}")

        retry_response = client.post(
            f"/admin/onboarding/jobs/{job_id}/retry",
            json=onboarding_job_retry_payload(),
        )
        retried_job = client.get(f"/admin/onboarding/jobs/{job_id}")
        tenant_id = retried_job.json()["tenant_id"]
        config_response = client.get(f"/tenants/{tenant_id}/config")
        namespace = config_response.json()["vector_namespace"]
        kb_documents = retrieval.documents[namespace]

    assert response.status_code == 202
    assert failed_job.json()["status"] == "failed"
    assert failed_job.json()["error"] == "KB partial failure"
    assert retry_response.status_code == 202
    assert retried_job.json()["status"] == "succeeded"
    assert all(
        document.metadata["onboarding_session_id"]
        == "00000000-0000-0000-0000-000000000123"
        for document in kb_documents
    )
    assert not any(
        document.metadata["source"] == "website:https://hustlehq.example/about"
        for document in kb_documents
    )
    assert any(
        document.metadata["source"] == "website:https://hustlehq.example/old"
        for document in kb_documents
    )


def test_onboarding_job_provisions_missing_provider_project_metadata() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "onboarding-hustle-hq-default-projects"
    payload["business_profile"]["business_name"] = "Hustle HQ Provider Defaults"
    payload["provider_projects"] = {}

    with TestClient(app) as client:
        response = client.post(
            "/admin/onboarding/jobs",
            json=payload,
        )
        job_id = response.json()["job_id"]
        job_response = client.get(f"/admin/onboarding/jobs/{job_id}")
        tenant_id = job_response.json()["tenant_id"]
        config_response = client.get(f"/tenants/{tenant_id}/config")

    assert response.status_code == 202
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "succeeded"
    assert config_response.status_code == 200
    assert config_response.json()["llm_project_name"] == (
        "customer-service-hustle-hq-provider-defaults"
    )
    assert config_response.json()["langsmith_project"] == (
        "customer-service-hustle-hq-provider-defaults"
    )
    assert config_response.json()["llm_project_id"] is None
    assert config_response.json()["web_search_provider"] == "tavily"
    assert config_response.json()["web_search_project_name"] == (
        "hustle-hq-provider-defaults"
    )


def test_onboarding_job_rejects_invalid_admin_email_message() -> None:
    payload = onboarding_job_payload()
    payload["admin"]["username_email"] = "not-an-email"

    with TestClient(app) as client:
        response = client.post("/admin/onboarding/jobs", json=payload)

    assert response.status_code == 422
    assert (
        "value is not a valid email address: An email address must have an @-sign."
        in validation_messages(response)
    )


def test_failed_onboarding_job_can_be_retried_with_persisted_payload_and_token() -> None:
    payload = onboarding_job_payload()
    payload["idempotency_key"] = "retry-provider-failure-001"
    payload["business_profile"]["business_name"] = "Retry Provider Ltd"
    secret_writer = FakeTelegramSecretWriter()
    registrar = FakeTelegramWebhookRegistrar()

    with TestClient(app) as client:
        client.app.state.container.onboarding_jobs.telegram_secret_writer = secret_writer
        client.app.state.container.onboarding_jobs.telegram_webhook_registrar = registrar
        client.app.state.container.onboarding_jobs.provider_project_provisioner = (
            FailingProviderProjectProvisioner()
        )
        failed_response = client.post("/admin/onboarding/jobs", json=payload)
        failed_job_id = failed_response.json()["job_id"]
        failed_job = client.get(f"/admin/onboarding/jobs/{failed_job_id}")

        client.app.state.container.onboarding_jobs.provider_project_provisioner = (
            MetadataOnlyProviderProjectProvisioner()
        )
        retry_response = client.post(
            f"/admin/onboarding/jobs/{failed_job_id}/retry",
            json=onboarding_job_retry_payload("123456:retry-bot-token"),
        )
        retried_job = client.get(f"/admin/onboarding/jobs/{failed_job_id}")

    assert failed_response.status_code == 202
    assert failed_job.json()["status"] == "failed"
    assert failed_job.json()["error"] == "Provider project provisioning failed"
    assert retry_response.status_code == 202
    assert retry_response.json()["job_id"] == failed_job_id
    assert retry_response.json()["status"] == "accepted"
    assert retried_job.json()["status"] == "succeeded"
    assert retried_job.json()["tenant_slug"] == "retry-provider-ltd"
    assert secret_writer.secrets[0]["secret_name"] == "tenant-retry-provider-ltd-telegram"
    assert secret_writer.secrets[0]["bot_token"] == "123456:retry-bot-token"
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        secret_writer.secrets[0]["webhook_secret_token"],
    )
    assert registrar.registrations[0]["bot_token"] == "123456:retry-bot-token"
    assert registrar.registrations[0]["webhook_secret_token"] == (
        secret_writer.secrets[0]["webhook_secret_token"]
    )
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        registrar.registrations[0]["webhook_secret_token"],
    )


def test_onboarding_job_retry_rejects_non_failed_job() -> None:
    with TestClient(app) as client:
        response = client.post("/admin/onboarding/jobs", json=onboarding_job_payload())
        job_id = response.json()["job_id"]
        retry_response = client.post(
            f"/admin/onboarding/jobs/{job_id}/retry",
            json=onboarding_job_retry_payload(),
        )

    assert retry_response.status_code == 409
    assert retry_response.json()["detail"] == "Only failed onboarding jobs can be retried"


def test_onboarding_job_retry_rejects_extra_payload_fields() -> None:
    with TestClient(app) as client:
        failed_payload = onboarding_job_payload()
        failed_payload["idempotency_key"] = "retry-extra-field-001"
        failed_payload["business_profile"]["business_name"] = "Retry Extra Field Ltd"
        client.app.state.container.onboarding_jobs.retrieval = FailingRetrievalStore()
        failed_response = client.post("/admin/onboarding/jobs", json=failed_payload)
        failed_job_id = failed_response.json()["job_id"]

        retry_response = client.post(
            f"/admin/onboarding/jobs/{failed_job_id}/retry",
            json={
                **onboarding_job_retry_payload(),
                "idempotency_key": "retry-extra-field-001",
            },
        )

    assert retry_response.status_code == 422
    assert "Extra inputs are not permitted" in validation_messages(retry_response)


def test_get_tenant_returns_404_for_unknown_tenant() -> None:
    with TestClient(app) as client:
        response = client.get("/tenants/unknown-tenant")

    assert response.status_code == 404
    assert response.json()["detail"] == "Tenant not found"


def test_tenant_config_can_be_read_and_updated() -> None:
    with TestClient(app) as client:
        default_response = client.get("/tenants/tenant-a/config")
        update_response = client.put(
            "/tenants/tenant-a/config",
            json={
                "selected_plan": "enterprise",
                "enabled_features": ["telegram", "whatsapp"],
                "business_summary": "Use Tenant A's concise tone.",
                "llm_project_id": "proj_tenant_a",
                "llm_project_name": "customer-service-tenant-a",
                "langsmith_project": "customer-service-tenant-a",
                "llm_provider": "langchain-compatible",
                "llm_model": "deepseek-chat",
                "llm_base_url": "https://api.deepseek.com",
                "vector_provider": "pgvector",
                "vector_isolation_mode": "shared_collection",
                "vector_collection": "customer-service",
                "vector_namespace": "tenant-a",
                "telegram_secret_name": "tenant-a-telegram",
                "whatsapp_secret_name": "tenant-a-whatsapp",
                "web_search_provider": "tavily",
                "web_search_project_name": "customer-service-tenant-a",
            },
        )
        get_response = client.get("/tenants/tenant-a/config")

    assert default_response.status_code == 404
    assert default_response.json()["detail"] == "Tenant config not found"
    assert update_response.status_code == 200
    assert update_response.json()["tenant_id"] == "tenant-a"
    assert update_response.json()["selected_plan"] == "enterprise"
    assert update_response.json()["enabled_features"] == [
        "telegram",
        "whatsapp",
    ]
    assert update_response.json()["business_summary"] == (
        "Use Tenant A's concise tone."
    )
    assert get_response.status_code == 200
    assert get_response.json()["business_summary"] == (
        "Use Tenant A's concise tone."
    )
    assert get_response.json()["llm_project_id"] == "proj_tenant_a"
    assert get_response.json()["llm_provider"] == "langchain-compatible"
    assert get_response.json()["llm_model"] == "deepseek-chat"
    assert get_response.json()["llm_base_url"] == "https://api.deepseek.com"
    assert get_response.json()["telegram_secret_name"] == "tenant-a-telegram"
    assert get_response.json()["whatsapp_secret_name"] == "tenant-a-whatsapp"
    assert get_response.json()["web_search_provider"] == "tavily"
    assert get_response.json()["web_search_project_name"] == "customer-service-tenant-a"


def test_tenant_config_rejects_unknown_feature() -> None:
    with TestClient(app) as client:
        response = client.put(
            "/tenants/tenant-a/config",
            json={"enabled_features": ["telegram", "unknown_feature"]},
        )

    assert response.status_code == 422
    assert any(
        "Input should be 'multimedia', 'telegram' or 'whatsapp'"
        in message
        for message in validation_messages(response)
    )


def test_tenant_config_rejects_unknown_providers() -> None:
    with TestClient(app) as client:
        llm_response = client.put(
            "/tenants/tenant-a/config",
            json={"llm_provider": "custom-provider"},
        )
        vector_response = client.put(
            "/tenants/tenant-a/config",
            json={"vector_provider": "custom-vector"},
        )

    assert llm_response.status_code == 422
    assert any(
        "Input should be 'langchain-compatible' or 'openai'" in message
        for message in validation_messages(llm_response)
    )
    assert vector_response.status_code == 422
    assert any(
        "Input should be 'pgvector', 'pinecone' or 'qdrant'" in message
        for message in validation_messages(vector_response)
    )


def test_tenant_config_rejects_invalid_llm_base_url() -> None:
    with TestClient(app) as client:
        response = client.put(
            "/tenants/tenant-a/config",
            json={"llm_base_url": "not-a-url"},
        )

    assert response.status_code == 422
    assert any(
        "llm_base_url must be an absolute http(s) URL" in message
        for message in validation_messages(response)
    )


def test_conversation_state_can_be_read_and_updated() -> None:
    with TestClient(app) as client:
        webhook_response = client.post(
            "/webhooks/synthetic",
            json={
                "event_id": "api-event-2",
                "external_chat_id": "chat-2",
                "external_user_id": "user-2",
                "text": "Please explain a topic that is not in the knowledge base.",
            },
        )
        conversation_id = webhook_response.json()["conversation_id"]

        get_response = client.get(f"/conversations/{conversation_id}")
        update_response = client.patch(
            f"/conversations/{conversation_id}/state",
            json={
                "state": "HUMAN_ACTIVE",
                "reason": "Human support accepted the handoff",
            },
        )

    assert webhook_response.status_code == 200
    assert webhook_response.json()["low_confidence"] is True
    assert webhook_response.json()["state"] == "BOT_ACTIVE"
    assert get_response.status_code == 200
    assert get_response.json()["state"] == "BOT_ACTIVE"
    assert update_response.status_code == 200
    assert update_response.json()["state"] == "HUMAN_ACTIVE"


def test_explicit_human_request_updates_conversation_state() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/messages/customer",
            json={
                "event_id": "api-human-request-1",
                "external_chat_id": "chat-human-request-1",
                "external_user_id": "user-human-request-1",
                "text": "Can I speak to a human agent?",
            },
        )

    assert response.status_code == 200
    assert response.json()["state"] == "HUMAN_REQUESTED"
    assert response.json()["low_confidence"] is True


def test_telegram_webhook_receives_customer_message_and_sends_reply() -> None:
    sender = FakeTelegramSender()
    with TestClient(app) as client:
        seed_default_knowledge(
            client,
            "refunds.txt",
            "Refund requests can be submitted within 30 days of purchase.",
        )
        client.app.state.container.telegram_sender = sender
        response = client.post(
            "/webhooks/telegram",
            json={
                "update_id": 1001,
                "message": {
                    "message_id": 2002,
                    "chat": {"id": 3003},
                    "from": {"id": 4004, "first_name": "Ada"},
                    "text": "What is the refund policy?",
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["reply"]["citations"] == ["kb/refunds.txt#0000"]
    assert sender.sent_messages == [
        (
            "3003",
            "Refund requests can be submitted within 30 days of purchase.",
            "default",
        )
    ]


def test_telegram_webhook_uses_resolved_tenant_secret_token() -> None:
    resolver = FakeTelegramCredentialResolver(webhook_secret_token="tenant-secret")
    sender = FakeTelegramSender()

    with TestClient(app) as client:
        client.app.state.container.telegram_credentials = resolver
        client.app.state.container.telegram_sender = sender
        response = client.post(
            "/webhooks/telegram?tenant_id=tenant-a",
            headers={"X-Telegram-Bot-Api-Secret-Token": "tenant-secret"},
            json={
                "update_id": 1001,
                "message": {
                    "message_id": 2002,
                    "chat": {"id": 3003},
                    "from": {"id": 4004},
                    "text": "Unknown question?",
                },
            },
        )

    assert response.status_code == 200
    assert resolver.tenant_ids == ["tenant-a"]
    assert sender.sent_messages[0][2] == "tenant-a"


def test_telegram_webhook_rejects_invalid_resolved_secret_token() -> None:
    resolver = FakeTelegramCredentialResolver(webhook_secret_token="expected-secret")

    with TestClient(app) as client:
        client.app.state.container.telegram_credentials = resolver
        response = client.post(
            "/webhooks/telegram?tenant_id=tenant-a",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            json={"update_id": 1001},
        )

    assert response.status_code == 403


def test_telegram_webhook_ignores_non_text_updates() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            json={
                "update_id": 1001,
                "message": {
                    "message_id": 2002,
                    "chat": {"id": 3003},
                    "from": {"id": 4004},
                    "photo": [{"file_id": "photo-id"}],
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}


def test_whatsapp_webhook_verification_accepts_matching_verify_token() -> None:
    resolver = FakeWhatsAppCredentialResolver(verify_token="expected-token")

    with TestClient(app) as client:
        client.app.state.container.whatsapp_credentials = resolver
        response = client.get(
            "/webhooks/whatsapp",
            params={
                "tenant_id": "tenant-a",
                "hub.mode": "subscribe",
                "hub.challenge": "challenge-value",
                "hub.verify_token": "expected-token",
            },
        )

    assert response.status_code == 200
    assert response.text == "challenge-value"
    assert resolver.tenant_ids == ["tenant-a"]


def test_whatsapp_webhook_verification_rejects_invalid_verify_token() -> None:
    resolver = FakeWhatsAppCredentialResolver(verify_token="expected-token")

    with TestClient(app) as client:
        client.app.state.container.whatsapp_credentials = resolver
        response = client.get(
            "/webhooks/whatsapp",
            params={
                "tenant_id": "tenant-a",
                "hub.mode": "subscribe",
                "hub.challenge": "challenge-value",
                "hub.verify_token": "wrong-token",
            },
        )

    assert response.status_code == 403
    assert resolver.tenant_ids == ["tenant-a"]


def test_whatsapp_webhook_receives_customer_message_and_sends_reply() -> None:
    sender = FakeWhatsAppSender()
    with TestClient(app) as client:
        seed_default_knowledge(
            client,
            "refunds.txt",
            "Refund requests can be submitted within 30 days of purchase.",
        )
        client.app.state.container.whatsapp_sender = sender
        response = client.post(
            "/webhooks/whatsapp",
            json={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "contacts": [
                                        {
                                            "wa_id": "254700000001",
                                            "profile": {"name": "Ada"},
                                        }
                                    ],
                                    "messages": [
                                        {
                                            "from": "254700000001",
                                            "id": "wamid.1001",
                                            "type": "text",
                                            "text": {"body": "What is the refund policy?"},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["replies"][0]["citations"] == ["kb/refunds.txt#0000"]
    assert sender.sent_messages == [
        (
            "254700000001",
            "Refund requests can be submitted within 30 days of purchase.",
            "default",
        )
    ]


def test_whatsapp_webhook_ignores_non_text_updates() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp",
            json={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "messages": [
                                        {
                                            "from": "254700000001",
                                            "id": "wamid.1001",
                                            "type": "image",
                                            "image": {"id": "media-id"},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}
