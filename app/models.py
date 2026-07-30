from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator

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
KnowledgeIngestionStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]


class IncomingMessage(BaseModel):
    tenant_id: str = Field(default="default", min_length=1)
    event_id: str = Field(min_length=1)
    channel: Literal["synthetic", "telegram", "whatsapp"] = "synthetic"
    external_chat_id: str = Field(min_length=1)
    external_user_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=10_000)
    sender_name: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SupportReply(BaseModel):
    tenant_id: str = "default"
    conversation_id: UUID
    answer: str
    confidence: float
    citations: list[str]
    low_confidence: bool
    state: ConversationState


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

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_base_url(cls, value: str | None) -> str | None:
        return validate_optional_http_url(value)


class KnowledgeIngestionResult(BaseModel):
    tenant_id: str
    namespace: str
    filename: str
    content_type: str
    pages_read: int
    pages_with_text: int
    chunks_created: int
    chunk_ids: list[str]


class KnowledgeIngestionJob(BaseModel):
    job_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    status: KnowledgeIngestionStatus
    filename: str = Field(min_length=1, max_length=500)
    content_type: str = Field(min_length=1, max_length=500)
    object_bucket: str = Field(min_length=1, max_length=500)
    object_key: str = Field(min_length=1, max_length=1_000)
    object_etag: str | None = Field(default=None, max_length=500)
    pages_read: int = 0
    pages_with_text: int = 0
    chunks_created: int = 0
    chunk_ids: list[str] = Field(default_factory=list)
    error_message: str | None = Field(default=None, max_length=2_000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None


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
