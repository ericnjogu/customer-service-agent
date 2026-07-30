import logging
import re
from datetime import datetime, timezone
from uuid import UUID

from langchain_core.documents import Document

from app.models import (
    ConversationPromptMetadata,
    ConversationRecord,
    IncomingMessage,
    KnowledgeIngestionJob,
    KnowledgeIngestionResult,
    QuestionPlan,
    StoredMessage,
    TenantConfig,
    TenantPlan,
    TenantRecord,
)
from app.tenancy import DEFAULT_TENANT_PLAN, generate_tenant_id, normalize_tenant_id, tenant_slug

logger = logging.getLogger(__name__)

STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "do",
    "for",
    "how",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "the",
    "to",
    "what",
    "you",
    "your",
}


def normalize_token(token: str) -> str:
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {normalize_token(token) for token in tokens} - STOP_WORDS


class MemoryConversationRepository:
    def __init__(self) -> None:
        self.conversations: dict[tuple[str, str, str], ConversationRecord] = {}
        self.messages: dict[tuple[str, str], StoredMessage] = {}

    async def initialize(self) -> None:
        return None

    async def get_or_create(self, message: IncomingMessage) -> ConversationRecord:
        key = (message.tenant_id, message.channel, message.external_chat_id)
        if key not in self.conversations:
            self.conversations[key] = ConversationRecord(
                tenant_id=message.tenant_id,
                channel=message.channel,
                external_chat_id=message.external_chat_id,
                external_user_id=message.external_user_id,
            )
        return self.conversations[key]

    async def get_by_id(self, conversation_id: UUID) -> ConversationRecord | None:
        return next(
            (
                conversation
                for conversation in self.conversations.values()
                if conversation.id == conversation_id
            ),
            None,
        )

    async def update_state(
        self,
        conversation_id: UUID,
        *,
        state: str,
        reason: str | None = None,
    ) -> ConversationRecord:
        conversation = await self.get_by_id(conversation_id)
        if not conversation:
            raise KeyError(f"Conversation not found: {conversation_id}")

        updated = conversation.model_copy(
            update={
                "state": state,
            }
        )
        key = (updated.tenant_id, updated.channel, updated.external_chat_id)
        self.conversations[key] = updated
        return updated

    async def save_message(self, message: StoredMessage) -> bool:
        key = (message.tenant_id, message.event_id)
        if key in self.messages:
            return False
        self.messages[key] = message
        return True

    async def list_messages_since(
        self,
        conversation_id: UUID,
        since: datetime,
        limit: int,
    ) -> list[StoredMessage]:
        messages = sorted(
            (
                message
                for message in self.messages.values()
                if message.conversation_id == conversation_id and message.created_at >= since
            ),
            key=lambda message: message.created_at,
        )
        return messages[-limit:]

    async def minutes_since_previous_customer_message(
        self,
        conversation_id: UUID,
        current_event_id: str,
        current_received_at: datetime,
    ) -> int | None:
        previous_customer_message = max(
            (
                message
                for message in self.messages.values()
                if message.conversation_id == conversation_id
                and message.sender_type == "CUSTOMER"
                and message.event_id != current_event_id
            ),
            key=lambda message: message.created_at,
            default=None,
        )
        if previous_customer_message is None:
            return None

        return max(
            0,
            int(
                (current_received_at - previous_customer_message.created_at).total_seconds()
                // 60
            ),
        )


class MemoryRetrievalStore:
    def __init__(self) -> None:
        self.documents: dict[str, list[Document]] = {}

    async def initialize(self) -> None:
        return None

    async def upsert(self, documents: list[Document], namespace: str) -> None:
        self.documents.setdefault(namespace, []).extend(documents)

    async def search(self, query: str, namespace: str, limit: int = 4) -> list[Document]:
        query_tokens = tokenize(query)

        def score(document: Document) -> float:
            tokens = tokenize(document.page_content)
            return len(query_tokens & tokens) / max(len(query_tokens), 1)

        ranked = sorted(self.documents.get(namespace, []), key=score, reverse=True)
        return [document for document in ranked if score(document) > 0][:limit]


class MemoryTenantConfigRepository:
    def __init__(self, default_vector_collection: str = "customer-support") -> None:
        self.default_vector_collection = default_vector_collection
        self.configs: dict[str, TenantConfig] = {}

    async def initialize(self) -> None:
        return None

    async def get(self, tenant_id: str) -> TenantConfig:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        return self.configs.get(
            normalized_tenant_id,
            TenantConfig.with_defaults(
                normalized_tenant_id,
                vector_collection=self.default_vector_collection,
            ),
        )

    async def get_existing(self, tenant_id: str) -> TenantConfig | None:
        return self.configs.get(normalize_tenant_id(tenant_id))

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
    ) -> TenantConfig:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        existing = await self.get(normalized_tenant_id)
        updated = existing.model_copy(
            update={
                "selected_plan": selected_plan or existing.selected_plan,
                "enabled_features": (
                    enabled_features
                    if enabled_features is not None
                    else existing.enabled_features
                ),
                "answer_prompt_instructions": (
                    answer_prompt_instructions
                    if answer_prompt_instructions is not None
                    else existing.answer_prompt_instructions
                ),
                "planner_prompt_instructions": (
                    planner_prompt_instructions
                    if planner_prompt_instructions is not None
                    else existing.planner_prompt_instructions
                ),
                "llm_project_id": (
                    llm_project_id
                    if llm_project_id is not None
                    else existing.llm_project_id
                ),
                "llm_project_name": (
                    llm_project_name
                    if llm_project_name is not None
                    else existing.llm_project_name
                ),
                "langsmith_project": (
                    langsmith_project
                    if langsmith_project is not None
                    else existing.langsmith_project
                ),
                "llm_provider": (
                    llm_provider if llm_provider is not None else existing.llm_provider
                ),
                "llm_model": (llm_model if llm_model is not None else existing.llm_model),
                "llm_base_url": (
                    llm_base_url if llm_base_url is not None else existing.llm_base_url
                ),
                "vector_provider": (
                    vector_provider
                    if vector_provider is not None
                    else existing.vector_provider
                ),
                "vector_isolation_mode": (
                    vector_isolation_mode
                    if vector_isolation_mode is not None
                    else existing.vector_isolation_mode
                ),
                "vector_collection": (
                    vector_collection
                    if vector_collection is not None
                    else existing.vector_collection
                ),
                "vector_namespace": (
                    vector_namespace
                    if vector_namespace is not None
                    else existing.vector_namespace
                ),
                "telegram_secret_name": (
                    telegram_secret_name
                    if telegram_secret_name is not None
                    else existing.telegram_secret_name
                ),
                "whatsapp_secret_name": (
                    whatsapp_secret_name
                    if whatsapp_secret_name is not None
                    else existing.whatsapp_secret_name
                ),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.configs[normalized_tenant_id] = updated
        return updated


class MemoryTenantRepository:
    def __init__(self) -> None:
        self.tenants: dict[str, TenantRecord] = {}

    async def initialize(self) -> None:
        return None

    async def get(self, tenant_id: str) -> TenantRecord | None:
        return self.tenants.get(normalize_tenant_id(tenant_id))

    async def get_by_slug(self, slug: str) -> TenantRecord | None:
        normalized_slug = tenant_slug(slug)
        return next(
            (tenant for tenant in self.tenants.values() if tenant.slug == normalized_slug),
            None,
        )

    async def create(
        self,
        *,
        display_name: str,
        slug: str | None = None,
        selected_plan: TenantPlan | None = None,
    ) -> TenantRecord:
        tenant_id = generate_tenant_id()
        now = datetime.now(timezone.utc)
        tenant = TenantRecord(
            tenant_id=tenant_id,
            slug=tenant_slug(slug or display_name),
            display_name=display_name.strip(),
            selected_plan=selected_plan or DEFAULT_TENANT_PLAN,
            created_at=now,
            updated_at=now,
        )
        self.tenants[tenant_id] = tenant
        return tenant


class MemoryKnowledgeIngestionJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, KnowledgeIngestionJob] = {}

    async def initialize(self) -> None:
        return None

    async def create(
        self,
        *,
        job_id: str,
        tenant_id: str,
        filename: str,
        content_type: str,
        object_bucket: str,
        object_key: str,
        object_etag: str | None = None,
    ) -> KnowledgeIngestionJob:
        job = KnowledgeIngestionJob(
            job_id=job_id,
            tenant_id=normalize_tenant_id(tenant_id),
            status="PENDING",
            filename=filename,
            content_type=content_type,
            object_bucket=object_bucket,
            object_key=object_key,
            object_etag=object_etag,
        )
        self.jobs[job_id] = job
        logger.info(
            "Created memory knowledge ingestion job job_id=%s tenant_id=%s status=%s "
            "bucket=%s key=%s",
            job.job_id,
            job.tenant_id,
            job.status,
            job.object_bucket,
            job.object_key,
        )
        return job

    async def get(self, tenant_id: str, job_id: str) -> KnowledgeIngestionJob | None:
        job = self.jobs.get(job_id)
        if not job or job.tenant_id != normalize_tenant_id(tenant_id):
            return None
        return job

    async def get_by_id(self, job_id: str) -> KnowledgeIngestionJob | None:
        return self.jobs.get(job_id)

    async def mark_running(self, job_id: str) -> KnowledgeIngestionJob:
        existing = self.jobs[job_id]
        updated = existing.model_copy(
            update={
                "status": "RUNNING",
                "started_at": datetime.now(timezone.utc),
                "error_message": None,
            }
        )
        self.jobs[job_id] = updated
        logger.info(
            "Marked memory knowledge ingestion job running job_id=%s tenant_id=%s",
            updated.job_id,
            updated.tenant_id,
        )
        return updated

    async def mark_succeeded(
        self,
        job_id: str,
        *,
        result: KnowledgeIngestionResult,
    ) -> KnowledgeIngestionJob:
        existing = self.jobs[job_id]
        updated = existing.model_copy(
            update={
                "status": "SUCCEEDED",
                "pages_read": result.pages_read,
                "pages_with_text": result.pages_with_text,
                "chunks_created": result.chunks_created,
                "chunk_ids": result.chunk_ids,
                "error_message": None,
                "finished_at": datetime.now(timezone.utc),
            }
        )
        self.jobs[job_id] = updated
        logger.info(
            "Marked memory knowledge ingestion job succeeded job_id=%s tenant_id=%s "
            "chunks_created=%d",
            updated.job_id,
            updated.tenant_id,
            updated.chunks_created,
        )
        return updated

    async def mark_failed(self, job_id: str, *, error_message: str) -> KnowledgeIngestionJob:
        existing = self.jobs[job_id]
        updated = existing.model_copy(
            update={
                "status": "FAILED",
                "error_message": error_message[:2_000],
                "finished_at": datetime.now(timezone.utc),
            }
        )
        self.jobs[job_id] = updated
        logger.info(
            "Marked memory knowledge ingestion job failed job_id=%s tenant_id=%s error=%s",
            updated.job_id,
            updated.tenant_id,
            updated.error_message,
        )
        return updated


class ExtractiveAnswerGenerator:
    async def generate(
        self,
        query: str,
        documents: list[Document],
        conversation_history: list[StoredMessage] | None = None,
        conversation_metadata: ConversationPromptMetadata | None = None,
        tenant_config: TenantConfig | None = None,
    ) -> tuple[str, float]:
        if not documents:
            return "I could not find enough information to answer that safely.", 0.0
        source = documents[0]
        answer = source.page_content
        overlap = len(tokenize(query) & tokenize(answer)) / max(len(tokenize(query)), 1)
        confidence = min(0.95, 0.55 + overlap)
        return answer, confidence

class RuleBasedHumanRequestDetector:
    human_request_phrases = (
        "human agent",
        "human support",
        "real person",
        "talk to someone",
        "speak to someone",
        "support team member",
        "customer support agent",
        "manager",
    )

    async def detect(
        self,
        message: IncomingMessage,
        conversation_history: list[StoredMessage] | None = None,
    ) -> bool:
        text = message.text.lower()
        return any(phrase in text for phrase in self.human_request_phrases)


class RuleBasedQuestionPlanner:
    out_of_scope_phrases = (
        "tell me a riddle",
        "write code",
        "solve this",
        "calculate",
    )
    history_cues = (
        "that",
        "this",
        "those",
        "it",
        "again",
        "previous",
        "earlier",
        "still",
        "same",
    )
    conversation_history_phrases = (
        "first message",
        "what message",
        "what time",
        "when did i",
        "what did i ask",
        "what did you say",
        "earlier",
        "before",
    )

    async def plan(
        self,
        message: IncomingMessage,
        conversation_metadata: ConversationPromptMetadata | None = None,
        tenant_config: TenantConfig | None = None,
    ) -> QuestionPlan:
        text = message.text.lower().strip()
        if self._is_arithmetic_question(text) or any(
            phrase in text for phrase in self.out_of_scope_phrases
        ):
            return QuestionPlan(
                in_scope=False,
                needs_conversation_history=False,
                explanation=(
                    "I can help with questions about this business, its services, "
                    "orders, bookings, policies, or support."
                ),
            )

        return QuestionPlan(
            in_scope=True,
            needs_conversation_history=(
                any(cue in tokenize(text) for cue in self.history_cues)
                or any(phrase in text for phrase in self.conversation_history_phrases)
            ),
            explanation="local heuristic planner",
        )

    def _is_arithmetic_question(self, text: str) -> bool:
        arithmetic_expression = r"[\d\s.,()+\-*/x×÷=%?]+"
        if re.fullmatch(arithmetic_expression, text):
            return bool(re.search(r"\d", text) and re.search(r"[+\-*/x×÷=%]", text))

        math_prefix = r"(what\s+is|what's|calculate|compute|solve)"
        return bool(re.fullmatch(rf"{math_prefix}\s+{arithmetic_expression}", text))
