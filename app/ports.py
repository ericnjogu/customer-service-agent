from datetime import datetime
from typing import Protocol
from uuid import UUID

from langchain_core.documents import Document

from app.models import (
    ConversationPromptMetadata,
    ConversationRecord,
    IncomingMessage,
    QuestionPlan,
    StoredMessage,
    TenantConfig,
    TenantPlan,
    TenantRecord,
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

    async def search(self, query: str, namespace: str, limit: int = 4) -> list[Document]: ...


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
