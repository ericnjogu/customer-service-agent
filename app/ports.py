from datetime import datetime
from typing import Protocol
from uuid import UUID

from langchain_core.documents import Document

from app.models import (
    BusinessContactPointRecord,
    BusinessProfileRecord,
    ConversationPromptMetadata,
    ConversationRecord,
    IncomingMessage,
    OnboardingAdmin,
    OnboardingBusinessProfile,
    OnboardingContactPoint,
    OnboardingEmailVerificationDiagnostic,
    OnboardingJobRecord,
    OnboardingProviderProjects,
    OnboardingSessionRecord,
    OnboardingSessionUpdate,
    OnboardingTelegramSetup,
    QuestionPlan,
    StoredMessage,
    TenantConfig,
    TenantMembershipRecord,
    TenantPlan,
    TenantRecord,
    WebsiteAnalysisResult,
)


class ConversationRepository(Protocol):
    async def initialize(self) -> None: ...

    async def get_or_create(self, message: IncomingMessage) -> ConversationRecord: ...

    async def get_by_id(self, conversation_id: UUID) -> ConversationRecord | None: ...

    async def update_state(
        self,
        conversation_id: UUID,
        *,
        state: str,
        reason: str | None = None,
    ) -> ConversationRecord: ...

    async def save_message(self, message: StoredMessage) -> bool: ...

    async def list_messages_since(
        self,
        conversation_id: UUID,
        since: datetime,
        limit: int,
    ) -> list[StoredMessage]: ...

    async def minutes_since_previous_customer_message(
        self,
        conversation_id: UUID,
        current_event_id: str,
        current_received_at: datetime,
    ) -> int | None: ...


class RetrievalStore(Protocol):
    async def initialize(self) -> None: ...

    async def upsert(self, documents: list[Document], namespace: str) -> None: ...

    async def delete_by_metadata(
        self,
        namespace: str,
        metadata_key: str,
        metadata_value: str,
    ) -> None: ...

    async def search(self, query: str, namespace: str, limit: int = 4) -> list[Document]: ...


class WebsiteAnalyzer(Protocol):
    async def analyze(self, session: OnboardingSessionRecord) -> WebsiteAnalysisResult: ...


class ProviderProjectProvisioner(Protocol):
    async def provision(
        self,
        session: OnboardingSessionRecord,
    ) -> OnboardingProviderProjects: ...

    async def provision_for(
        self,
        *,
        business_name: str,
        website_url: str,
        provider_projects: OnboardingProviderProjects,
        session_id: UUID | None = None,
    ) -> OnboardingProviderProjects: ...


class TenantConfigRepository(Protocol):
    async def initialize(self) -> None: ...

    async def get(self, tenant_id: str) -> TenantConfig: ...

    async def get_existing(self, tenant_id: str) -> TenantConfig | None: ...

    async def upsert(
        self,
        tenant_id: str,
        *,
        selected_plan: TenantPlan | None = None,
        enabled_features: list[str] | None = None,
        answer_prompt_instructions: str | None = None,
        planner_prompt_instructions: str | None = None,
        llm_project_id: str | None = None,
        llm_project_name: str | None = None,
        langsmith_project: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        vector_provider: str | None = None,
        vector_isolation_mode: str | None = None,
        vector_collection: str | None = None,
        vector_namespace: str | None = None,
        telegram_secret_name: str | None = None,
        whatsapp_secret_name: str | None = None,
        web_search_provider: str | None = None,
        web_search_project_name: str | None = None,
    ) -> TenantConfig: ...


class TenantRepository(Protocol):
    async def initialize(self) -> None: ...

    async def get(self, tenant_id: str) -> TenantRecord | None: ...

    async def get_by_slug(self, slug: str) -> TenantRecord | None: ...

    async def create(
        self,
        *,
        display_name: str,
        slug: str | None = None,
        selected_plan: TenantPlan | None = None,
    ) -> TenantRecord: ...


class OnboardingRepository(Protocol):
    async def initialize(self) -> None: ...

    async def create_job(
        self,
        *,
        idempotency_key: str,
        request_payload: dict,
    ) -> OnboardingJobRecord: ...

    async def get_job(self, job_id: UUID) -> OnboardingJobRecord | None: ...

    async def get_job_payload(self, job_id: UUID) -> dict | None: ...

    async def get_job_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> OnboardingJobRecord | None: ...

    async def mark_job_accepted(self, job_id: UUID) -> OnboardingJobRecord: ...

    async def mark_job_running(self, job_id: UUID) -> OnboardingJobRecord: ...

    async def mark_job_succeeded(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        tenant_slug: str,
    ) -> OnboardingJobRecord: ...

    async def mark_job_failed(
        self,
        job_id: UUID,
        *,
        error: str,
        tenant_id: str | None = None,
        tenant_slug: str | None = None,
    ) -> OnboardingJobRecord: ...

    async def save_business_profile(
        self,
        tenant_id: str,
        profile: OnboardingBusinessProfile,
    ) -> BusinessProfileRecord: ...

    async def replace_contact_points(
        self,
        tenant_id: str,
        contact_points: list[OnboardingContactPoint],
    ) -> list[BusinessContactPointRecord]: ...

    async def save_owner_membership(
        self,
        tenant_id: str,
        admin: OnboardingAdmin,
    ) -> TenantMembershipRecord: ...

    async def create_session(
        self,
        *,
        admin: OnboardingAdmin,
        terms_version: str,
        terms_accepted_at: datetime,
    ) -> OnboardingSessionRecord: ...

    async def save_session_website(
        self,
        session_id: UUID,
        *,
        website_url: str,
        website_verification_email: str,
    ) -> OnboardingSessionRecord: ...

    async def get_active_session_by_website_domain(
        self,
        website_domain: str,
    ) -> OnboardingSessionRecord | None: ...

    async def get_business_profile_by_website_domain(
        self,
        website_domain: str,
    ) -> BusinessProfileRecord | None: ...

    async def get_session(self, session_id: UUID) -> OnboardingSessionRecord | None: ...

    async def update_session(
        self,
        session_id: UUID,
        update: OnboardingSessionUpdate,
    ) -> OnboardingSessionRecord: ...

    async def save_session_analysis(
        self,
        session_id: UUID,
        *,
        analysis: WebsiteAnalysisResult,
    ) -> OnboardingSessionRecord: ...

    async def save_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord: ...

    async def consume_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool: ...

    async def inspect_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> OnboardingEmailVerificationDiagnostic: ...

    async def save_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord: ...

    async def consume_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool: ...

    async def inspect_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> OnboardingEmailVerificationDiagnostic: ...

    async def save_telegram_setup_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        setup_url: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord: ...

    async def consume_telegram_setup_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool: ...

    async def save_telegram_setup(
        self,
        session_id: UUID,
        telegram: OnboardingTelegramSetup,
    ) -> OnboardingSessionRecord: ...

    async def save_provider_projects(
        self,
        session_id: UUID,
        provider_projects: OnboardingProviderProjects,
    ) -> OnboardingSessionRecord: ...

    async def mark_session_submitted(
        self,
        session_id: UUID,
        *,
        job_id: UUID,
    ) -> OnboardingSessionRecord: ...

    async def mark_session_failed(
        self,
        session_id: UUID,
        *,
        error: str,
    ) -> OnboardingSessionRecord: ...


class EmbeddingProvider(Protocol):
    @property
    def dimensions(self) -> int: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class AnswerGenerator(Protocol):
    async def generate(
        self,
        query: str,
        documents: list[Document],
        conversation_history: list[StoredMessage] | None = None,
        conversation_metadata: ConversationPromptMetadata | None = None,
        tenant_config: TenantConfig | None = None,
    ) -> tuple[str, float]: ...


class QuestionPlanner(Protocol):
    async def plan(
        self,
        message: IncomingMessage,
        conversation_metadata: ConversationPromptMetadata | None = None,
        tenant_config: TenantConfig | None = None,
    ) -> QuestionPlan: ...
