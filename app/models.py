import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

import phonenumbers
from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator

from app.tenancy import (
    DEFAULT_TENANT_PLAN,
    DEFAULT_VECTOR_COLLECTION,
    DEFAULT_VECTOR_ISOLATION_MODE,
    DEFAULT_VECTOR_PROVIDER,
    default_langsmith_project,
    default_llm_project_name,
    default_vector_namespace,
    normalize_tenant_id,
)

ConversationState = Literal["BOT_ACTIVE", "HUMAN_REQUESTED", "HUMAN_ACTIVE"]
TenantPlan = Literal["sme", "enterprise"]
TenantFeature = Literal["multimedia", "telegram", "whatsapp"]
LlmProvider = Literal["langchain-compatible", "openai"]
VectorProvider = Literal["pgvector", "pinecone", "qdrant"]
VectorIsolationMode = Literal["shared_collection", "dedicated_collection"]
TenantMemberRole = Literal["owner", "admin", "agent"]
OnboardingJobStatus = Literal["accepted", "running", "succeeded", "failed"]
OnboardingSessionStatus = Literal[
    "username_email_verification_pending",
    "website_verification_pending",
    "draft",
    "ready_for_review",
    "awaiting_telegram_setup",
    "ready_to_submit",
    "submitted",
    "failed",
]
ONBOARDING_TERMS_VERSION = "beta-2026-08-28"


class IncomingMessage(BaseModel):
    tenant_id: str = Field(default="default", min_length=1)
    event_id: str = Field(min_length=1)
    channel: Literal["synthetic", "telegram", "whatsapp"] = "synthetic"
    external_chat_id: str = Field(min_length=1)
    external_user_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=10_000)
    sender_name: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ServiceReply(BaseModel):
    tenant_id: str = "default"
    conversation_id: UUID
    answer: str
    confidence: float
    citations: list[str]
    low_confidence: bool
    state: ConversationState


@dataclass(frozen=True)
class AnswerGenerationResult:
    answer: str
    confidence: float
    answer_found: bool = True
    grounded: bool = True

    def __iter__(self):
        yield self.answer
        yield self.confidence


class RuntimeWebSearchSource(BaseModel):
    url: str = Field(min_length=1, max_length=1_000)
    title: str | None = Field(default=None, max_length=500)
    text: str = Field(default="", max_length=20_000)
    provider: str = Field(default="unknown", min_length=1, max_length=100)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RuntimeWebSearchResult(BaseModel):
    answer: str = ""
    sources: list[RuntimeWebSearchSource] = Field(default_factory=list)


class ConversationRecord(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "default"
    channel: str
    external_chat_id: str
    external_user_id: str
    state: ConversationState = "BOT_ACTIVE"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StoredMessage(BaseModel):
    tenant_id: str = "default"
    conversation_id: UUID
    event_id: str
    sender_type: Literal["CUSTOMER", "BOT", "AGENT", "SYSTEM"]
    body: str
    in_scope: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ConversationPromptMetadata(BaseModel):
    is_first_customer_message: bool
    customer_name: str | None = None
    minutes_since_last_customer_message: int | None = None
    should_greet_customer: bool
    greeting_reason: str | None = None


class QuestionPlan(BaseModel):
    in_scope: bool = True
    needs_conversation_history: bool = True
    explicit_human_request: bool = False
    explanation: str | None = None


class TenantRecord(BaseModel):
    tenant_id: str = Field(min_length=1)
    slug: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=500)
    selected_plan: TenantPlan = DEFAULT_TENANT_PLAN
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TenantCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=500)
    slug: str | None = Field(default=None, max_length=500)
    selected_plan: TenantPlan = DEFAULT_TENANT_PLAN


class TenantConfig(BaseModel):
    tenant_id: str = Field(min_length=1)
    selected_plan: TenantPlan = DEFAULT_TENANT_PLAN
    enabled_features: list[TenantFeature] = Field(default_factory=list)
    answer_prompt_instructions: str | None = Field(default=None, max_length=10_000)
    planner_prompt_instructions: str | None = Field(default=None, max_length=10_000)
    llm_project_id: str | None = Field(default=None, max_length=500)
    llm_project_name: str | None = Field(default=None, max_length=500)
    langsmith_project: str | None = Field(default=None, max_length=500)
    llm_provider: LlmProvider | None = None
    llm_model: str | None = Field(default=None, max_length=500)
    llm_base_url: str | None = Field(default=None, max_length=1_000)
    vector_provider: VectorProvider = DEFAULT_VECTOR_PROVIDER
    vector_isolation_mode: VectorIsolationMode = DEFAULT_VECTOR_ISOLATION_MODE
    vector_collection: str = Field(
        default=DEFAULT_VECTOR_COLLECTION,
        min_length=1,
        max_length=500,
    )
    vector_namespace: str | None = Field(default=None, max_length=500)
    telegram_secret_name: str | None = Field(default=None, max_length=500)
    whatsapp_secret_name: str | None = Field(default=None, max_length=500)
    web_search_provider: str | None = Field(default=None, max_length=100)
    web_search_project_name: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_base_url(cls, value: str | None) -> str | None:
        return validate_optional_http_url(value)

    @classmethod
    def with_defaults(
        cls,
        tenant_id: str,
        *,
        selected_plan: TenantPlan = DEFAULT_TENANT_PLAN,
        enabled_features: list[TenantFeature] | None = None,
        answer_prompt_instructions: str | None = None,
        planner_prompt_instructions: str | None = None,
        llm_project_id: str | None = None,
        llm_project_name: str | None = None,
        langsmith_project: str | None = None,
        llm_provider: LlmProvider | None = None,
        llm_model: str | None = None,
        llm_base_url: str | None = None,
        vector_provider: VectorProvider = DEFAULT_VECTOR_PROVIDER,
        vector_isolation_mode: VectorIsolationMode = DEFAULT_VECTOR_ISOLATION_MODE,
        vector_collection: str = DEFAULT_VECTOR_COLLECTION,
        vector_namespace: str | None = None,
        telegram_secret_name: str | None = None,
        whatsapp_secret_name: str | None = None,
        web_search_provider: str | None = None,
        web_search_project_name: str | None = None,
    ) -> "TenantConfig":
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        return cls(
            tenant_id=normalized_tenant_id,
            selected_plan=selected_plan,
            enabled_features=enabled_features or [],
            answer_prompt_instructions=answer_prompt_instructions,
            planner_prompt_instructions=planner_prompt_instructions,
            llm_project_id=llm_project_id,
            llm_project_name=(
                llm_project_name or default_llm_project_name(normalized_tenant_id)
            ),
            langsmith_project=(
                langsmith_project or default_langsmith_project(normalized_tenant_id)
            ),
            llm_provider=llm_provider,
            llm_model=llm_model,
            llm_base_url=llm_base_url,
            vector_provider=vector_provider,
            vector_isolation_mode=vector_isolation_mode,
            vector_collection=vector_collection,
            vector_namespace=vector_namespace or default_vector_namespace(normalized_tenant_id),
            telegram_secret_name=telegram_secret_name,
            whatsapp_secret_name=whatsapp_secret_name,
            web_search_provider=web_search_provider,
            web_search_project_name=web_search_project_name,
        )


class TenantConfigUpdate(BaseModel):
    selected_plan: TenantPlan | None = None
    enabled_features: list[TenantFeature] | None = None
    answer_prompt_instructions: str | None = Field(default=None, max_length=10_000)
    planner_prompt_instructions: str | None = Field(default=None, max_length=10_000)
    llm_project_id: str | None = Field(default=None, max_length=500)
    llm_project_name: str | None = Field(default=None, max_length=500)
    langsmith_project: str | None = Field(default=None, max_length=500)
    llm_provider: LlmProvider | None = None
    llm_model: str | None = Field(default=None, max_length=500)
    llm_base_url: str | None = Field(default=None, max_length=1_000)
    vector_provider: VectorProvider | None = None
    vector_isolation_mode: VectorIsolationMode | None = None
    vector_collection: str | None = Field(default=None, max_length=500)
    vector_namespace: str | None = Field(default=None, max_length=500)
    telegram_secret_name: str | None = Field(default=None, max_length=500)
    whatsapp_secret_name: str | None = Field(default=None, max_length=500)
    web_search_provider: str | None = Field(default=None, max_length=100)
    web_search_project_name: str | None = Field(default=None, max_length=500)

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_base_url(cls, value: str | None) -> str | None:
        return validate_optional_http_url(value)


def validate_optional_http_url(value: str | None) -> str | None:
    if value is None:
        return None

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("llm_base_url must be an absolute http(s) URL")
    return value


class ConversationStateUpdate(BaseModel):
    state: ConversationState
    reason: str | None = Field(default=None, max_length=1_000)


def normalize_onboarding_admin_payload(data: dict) -> dict:
    normalized = dict(data)
    if "username_email" not in normalized and "email" in normalized:
        normalized["username_email"] = normalized["email"]
    if ("given_name" not in normalized or "family_name" not in normalized) and normalized.get(
        "name"
    ):
        name_parts = str(normalized["name"]).strip().split(None, 1)
        if "given_name" not in normalized and name_parts:
            normalized["given_name"] = name_parts[0]
        if "family_name" not in normalized and len(name_parts) > 1:
            normalized["family_name"] = name_parts[1]
    return normalized


class OnboardingAdmin(BaseModel):
    username_email: EmailStr
    given_name: str = Field(min_length=1, max_length=200)
    family_name: str = Field(min_length=1, max_length=200)
    phone_number: str = Field(min_length=1, max_length=100)
    role_title: str = Field(min_length=1, max_length=200)
    authority_confirmed: bool
    terms_accepted: bool

    @field_validator("given_name", "family_name", mode="before")
    @classmethod
    def normalize_required_name(cls, value: object) -> str:
        return str(value).strip() if value is not None else ""

    @classmethod
    def model_validate(cls, obj: object, *args, **kwargs):  # type: ignore[override]
        if isinstance(obj, dict):
            obj = normalize_onboarding_admin_payload(obj)
        return super().model_validate(obj, *args, **kwargs)

    def __init__(self, **data):
        super().__init__(**normalize_onboarding_admin_payload(data))

    @property
    def name(self) -> str:
        return f"{self.given_name} {self.family_name}".strip()

    @property
    def email(self) -> EmailStr:
        return self.username_email

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: str) -> str:
        normalized = value.strip()
        try:
            parsed = phonenumbers.parse(normalized, None)
        except phonenumbers.NumberParseException as error:
            raise ValueError("phone_number must be in international format") from error
        if (
            not normalized.startswith("+")
            or not phonenumbers.is_possible_number(parsed)
            or not phonenumbers.is_valid_number(parsed)
        ):
            raise ValueError("phone_number must be in international format")
        return phonenumbers.format_number(
            parsed,
            phonenumbers.PhoneNumberFormat.E164,
        )

    @field_validator("authority_confirmed")
    @classmethod
    def validate_authority_confirmed(cls, value: bool) -> bool:
        if not value:
            raise ValueError("authority_confirmed must be accepted")
        return value

    @field_validator("terms_accepted")
    @classmethod
    def validate_terms_accepted(cls, value: bool) -> bool:
        if not value:
            raise ValueError("terms_accepted must be accepted")
        return value


class OnboardingBusinessProfile(BaseModel):
    business_name: str = Field(default="", max_length=500)
    website_url: HttpUrl
    location_name: str = Field(default="", max_length=500)
    physical_location: str = Field(default="", max_length=1_000)
    business_phone: str = Field(default="", max_length=100)
    business_email: str = Field(default="", max_length=500)
    google_place_url: HttpUrl | None = None

    @field_validator("business_email")
    @classmethod
    def validate_optional_business_email(cls, value: str) -> str:
        if not value:
            return value
        EmailStr._validate(value)
        return value


class OnboardingContactPoint(BaseModel):
    kind: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=200)
    value: str | None = Field(default=None, max_length=1_000)
    url: str | None = Field(default=None, max_length=1_000)
    is_primary: bool = False

    @field_validator("url", mode="before")
    @classmethod
    def validate_contact_url(cls, value: object) -> str | None:
        if value is None:
            return None

        normalized = str(value).strip()
        if not normalized:
            return None
        if re.search(r"\s", normalized):
            raise ValueError("contact url must not contain whitespace")

        parsed = urlparse(normalized)
        if not parsed.scheme:
            raise ValueError("contact url must include a URI scheme")
        if parsed.scheme in {"http", "https"} and not parsed.netloc:
            raise ValueError("contact http(s) url must include a host")
        return normalized


class OnboardingTelegramSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: str = Field(min_length=1, max_length=2_000)
    webhook_secret_token: str = Field(min_length=1, max_length=2_000)


class OnboardingProviderProjects(BaseModel):
    llm_project_id: str | None = Field(default=None, max_length=500)
    llm_project_name: str | None = Field(default=None, max_length=500)
    langsmith_project: str | None = Field(default=None, max_length=500)
    web_search_provider: str | None = Field(default=None, max_length=100)
    web_search_project_name: str | None = Field(default=None, max_length=500)


class WebsiteResearchSource(BaseModel):
    url: str = Field(min_length=1, max_length=1_000)
    title: str | None = Field(default=None, max_length=500)
    text: str = Field(default="", max_length=20_000)
    provider: str = Field(default="unknown", min_length=1, max_length=100)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WebsiteResearchResult(BaseModel):
    notes: str = ""
    sources: list[WebsiteResearchSource] = Field(default_factory=list)


class OnboardingSessionCreate(BaseModel):
    admin: OnboardingAdmin


class WebsiteAnalysisResult(BaseModel):
    business_profile: OnboardingBusinessProfile
    agent_name: str = Field(default="", max_length=500)
    agent_description: str = Field(default="", max_length=1_000)
    answer_prompt_instructions: str = Field(default="", max_length=10_000)
    contact_info: list[OnboardingContactPoint] = Field(default_factory=list)
    knowledge_sources: list[WebsiteResearchSource] = Field(default_factory=list)


class OnboardingSessionUpdate(BaseModel):
    current_step: str | None = Field(default=None, max_length=100)
    business_profile: OnboardingBusinessProfile | None = None
    agent_name: str | None = Field(default=None, max_length=500)
    agent_description: str | None = Field(default=None, max_length=1_000)
    answer_prompt_instructions: str | None = Field(default=None, max_length=10_000)
    contact_info: list[OnboardingContactPoint] | None = None
    provider_projects: OnboardingProviderProjects | None = None
    knowledge_sources: list[WebsiteResearchSource] | None = None


class OnboardingTelegramSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=2_000)
    bot_token: str = Field(min_length=1, max_length=2_000)


class OnboardingEmailVerificationRequest(BaseModel):
    token: str = Field(min_length=1, max_length=2_000)


class OnboardingEmailVerificationDiagnostic(BaseModel):
    session_exists: bool = True
    admin_email_verified: bool = False
    has_token_hash: bool = False
    token_matches: bool = False
    token_used: bool = False
    token_expired: bool = False
    expires_at: datetime | None = None
    used_at: datetime | None = None
    submitted_token_fingerprint: str | None = None
    stored_token_fingerprint: str | None = None


class OnboardingSessionWebsiteRequest(BaseModel):
    website_url: HttpUrl
    website_verification_email: EmailStr


class OnboardingSessionRecord(BaseModel):
    session_id: UUID = Field(default_factory=uuid4)
    status: OnboardingSessionStatus = "username_email_verification_pending"
    current_step: str = "username-email-verification"
    website_url: str | None = None
    website_verification_email: EmailStr | None = None
    admin: OnboardingAdmin
    terms_version: str | None = Field(default=None, max_length=100)
    terms_accepted_at: datetime | None = None
    username_email_verified: bool = False
    username_email_verification_expires_at: datetime | None = None
    website_email_verified: bool = False
    website_email_verification_expires_at: datetime | None = None
    analysis: WebsiteAnalysisResult | None = None
    business_profile: OnboardingBusinessProfile | None = None
    agent_name: str | None = None
    agent_description: str | None = None
    answer_prompt_instructions: str | None = None
    contact_info: list[OnboardingContactPoint] = Field(default_factory=list)
    telegram: OnboardingTelegramSetup | None = None
    provider_projects: OnboardingProviderProjects = Field(
        default_factory=OnboardingProviderProjects
    )
    knowledge_sources: list[WebsiteResearchSource] = Field(default_factory=list)
    telegram_setup_url: str | None = None
    telegram_setup_token_expires_at: datetime | None = None
    submitted_job_id: UUID | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OnboardingJobCreate(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=500)
    callback_url: HttpUrl | None = None
    selected_plan: TenantPlan = DEFAULT_TENANT_PLAN
    admin: OnboardingAdmin
    business_profile: OnboardingBusinessProfile
    agent_name: str = Field(min_length=1, max_length=500)
    agent_description: str = Field(min_length=1, max_length=1_000)
    answer_prompt_instructions: str = Field(min_length=1, max_length=10_000)
    contact_info: list[OnboardingContactPoint] = Field(default_factory=list)
    telegram: OnboardingTelegramSetup
    provider_projects: OnboardingProviderProjects = Field(
        default_factory=OnboardingProviderProjects
    )
    knowledge_sources: list[WebsiteResearchSource] = Field(default_factory=list)


class OnboardingJobRetryTelegram(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bot_token: str = Field(min_length=1, max_length=2_000)


class OnboardingJobRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram: OnboardingJobRetryTelegram


class OnboardingJobRecord(BaseModel):
    job_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str
    status: OnboardingJobStatus = "accepted"
    tenant_id: str | None = None
    tenant_slug: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OnboardingJobAccepted(BaseModel):
    job_id: UUID
    status: OnboardingJobStatus


class BusinessProfileRecord(BaseModel):
    tenant_id: str
    business_name: str
    website_url: str
    location_name: str
    physical_location: str
    business_phone: str
    business_email: str
    google_place_url: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class BusinessContactPointRecord(BaseModel):
    tenant_id: str
    kind: str
    label: str | None = None
    value: str | None = None
    url: str | None = None
    is_primary: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TenantMembershipRecord(BaseModel):
    tenant_id: str
    user_email: str
    user_name: str
    role: TenantMemberRole = "owner"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
