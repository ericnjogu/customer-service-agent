import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

from langchain_core.documents import Document

from app.models import (
    AnswerGenerationResult,
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


def normalize_website_domain(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.hostname or value
    return host.strip().lower().removeprefix("www.")


def token_fingerprint(token_hash: str | None) -> str | None:
    return token_hash[:12] if token_hash else None


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
                and message.in_scope
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

    async def upsert(self, documents: list[Document], tenant_id: str) -> None:
        existing = {
            str(document.metadata.get("chunk_id") or document.metadata.get("source")): document
            for document in self.documents.get(tenant_id, [])
        }
        for document in documents:
            key = str(document.metadata.get("chunk_id") or document.metadata.get("source"))
            existing[key] = document
        self.documents[tenant_id] = list(existing.values())

    async def delete_by_metadata(
        self,
        tenant_id: str,
        metadata_key: str,
        metadata_value: str,
    ) -> None:
        self.documents[tenant_id] = [
            document
            for document in self.documents.get(tenant_id, [])
            if str(document.metadata.get(metadata_key) or "") != metadata_value
        ]

    async def delete_by_source_url(self, tenant_id: str, source_url: str) -> None:
        await self.delete_by_metadata(tenant_id, "source_url", source_url)

    async def search(self, query: str, tenant_id: str, limit: int = 4) -> list[Document]:
        query_tokens = tokenize(query)

        def score(document: Document) -> float:
            tokens = tokenize(document.page_content)
            return len(query_tokens & tokens) / max(len(query_tokens), 1)

        ranked = sorted(self.documents.get(tenant_id, []), key=score, reverse=True)
        return [document for document in ranked if score(document) > 0][:limit]


class MemoryTenantConfigRepository:
    def __init__(self, default_vector_collection: str = "customer-service") -> None:
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
        business_summary: str | None = None,
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
                "business_summary": (
                    business_summary
                    if business_summary is not None
                    else existing.business_summary
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
                "web_search_provider": (
                    web_search_provider
                    if web_search_provider is not None
                    else existing.web_search_provider
                ),
                "web_search_project_name": (
                    web_search_project_name
                    if web_search_project_name is not None
                    else existing.web_search_project_name
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


class MemoryOnboardingRepository:
    def __init__(self) -> None:
        self.jobs: dict[UUID, OnboardingJobRecord] = {}
        self.job_payloads: dict[UUID, dict] = {}
        self.job_idempotency_keys: dict[str, UUID] = {}
        self.business_profiles: dict[str, BusinessProfileRecord] = {}
        self.contact_points: dict[str, list[BusinessContactPointRecord]] = {}
        self.memberships: dict[tuple[str, str], TenantMembershipRecord] = {}
        self.sessions: dict[UUID, OnboardingSessionRecord] = {}
        self.session_username_email_token_hashes: dict[UUID, str] = {}
        self.session_username_email_token_used_at: dict[UUID, datetime] = {}
        self.session_website_email_token_hashes: dict[UUID, str] = {}
        self.session_website_email_token_used_at: dict[UUID, datetime] = {}
        self.session_token_hashes: dict[UUID, str] = {}
        self.session_token_used_at: dict[UUID, datetime] = {}

    async def initialize(self) -> None:
        return None

    async def create_job(
        self,
        *,
        idempotency_key: str,
        request_payload: dict,
    ) -> OnboardingJobRecord:
        existing = await self.get_job_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
        now = datetime.now(timezone.utc)
        job = OnboardingJobRecord(
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.job_id] = job
        self.job_payloads[job.job_id] = request_payload
        self.job_idempotency_keys[idempotency_key] = job.job_id
        return job

    async def get_job(self, job_id: UUID) -> OnboardingJobRecord | None:
        return self.jobs.get(job_id)

    async def get_job_payload(self, job_id: UUID) -> dict | None:
        return self.job_payloads.get(job_id)

    async def get_job_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> OnboardingJobRecord | None:
        job_id = self.job_idempotency_keys.get(idempotency_key)
        if job_id is None:
            return None
        return self.jobs.get(job_id)

    async def mark_job_accepted(self, job_id: UUID) -> OnboardingJobRecord:
        return await self._update_job(
            job_id,
            status="accepted",
            error=None,
        )

    async def mark_job_running(self, job_id: UUID) -> OnboardingJobRecord:
        return await self._update_job(job_id, status="running", error=None)

    async def mark_job_succeeded(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        tenant_slug: str,
    ) -> OnboardingJobRecord:
        return await self._update_job(
            job_id,
            status="succeeded",
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            error=None,
        )

    async def mark_job_failed(
        self,
        job_id: UUID,
        *,
        error: str,
        tenant_id: str | None = None,
        tenant_slug: str | None = None,
    ) -> OnboardingJobRecord:
        return await self._update_job(
            job_id,
            status="failed",
            error=error,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
        )

    async def save_business_profile(
        self,
        tenant_id: str,
        profile: OnboardingBusinessProfile,
    ) -> BusinessProfileRecord:
        now = datetime.now(timezone.utc)
        record = BusinessProfileRecord(
            tenant_id=tenant_id,
            business_name=profile.business_name,
            website_url=str(profile.website_url),
            location_name=profile.location_name,
            physical_location=profile.physical_location,
            business_phone=profile.business_phone,
            business_email=profile.business_email,
            google_place_url=(
                str(profile.google_place_url) if profile.google_place_url else None
            ),
            created_at=self.business_profiles.get(
                tenant_id,
                BusinessProfileRecord(
                    tenant_id=tenant_id,
                    business_name=profile.business_name,
                    website_url=str(profile.website_url),
                    location_name=profile.location_name,
                    physical_location=profile.physical_location,
                    business_phone=profile.business_phone,
                    business_email=profile.business_email,
                ),
            ).created_at,
            updated_at=now,
        )
        self.business_profiles[tenant_id] = record
        return record

    async def replace_contact_points(
        self,
        tenant_id: str,
        contact_points: list[OnboardingContactPoint],
    ) -> list[BusinessContactPointRecord]:
        records = [
            BusinessContactPointRecord(
                tenant_id=tenant_id,
                kind=point.kind,
                label=point.label,
                value=point.value,
                url=str(point.url) if point.url else None,
                is_primary=point.is_primary,
            )
            for point in contact_points
        ]
        self.contact_points[tenant_id] = records
        return records

    async def list_contact_points(
        self,
        tenant_id: str,
    ) -> list[BusinessContactPointRecord]:
        return list(self.contact_points.get(tenant_id, []))

    async def save_owner_membership(
        self,
        tenant_id: str,
        admin: OnboardingAdmin,
    ) -> TenantMembershipRecord:
        record = TenantMembershipRecord(
            tenant_id=tenant_id,
            user_email=admin.username_email,
            user_name=admin.name,
            role="owner",
        )
        self.memberships[(tenant_id, admin.username_email.lower())] = record
        return record

    async def create_session(
        self,
        *,
        admin: OnboardingAdmin,
        terms_version: str,
        terms_accepted_at: datetime,
    ) -> OnboardingSessionRecord:
        now = datetime.now(timezone.utc)
        session = OnboardingSessionRecord(
            session_id=uuid4(),
            admin=admin,
            terms_version=terms_version,
            terms_accepted_at=terms_accepted_at,
            status="username_email_verification_pending",
            current_step="username-email-verification",
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.session_id] = session
        return session

    async def save_session_website(
        self,
        session_id: UUID,
        *,
        website_url: str,
        website_verification_email: str,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        updated = session.model_copy(
            update={
                "website_url": website_url,
                "website_verification_email": website_verification_email,
                "website_email_verified": False,
                "status": "website_verification_pending",
                "current_step": "website-email-verification",
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def get_active_session_by_website_domain(
        self,
        website_domain: str,
    ) -> OnboardingSessionRecord | None:
        normalized_domain = normalize_website_domain(website_domain)
        return next(
            (
                session
                for session in self.sessions.values()
                if session.status != "failed"
                and session.website_url
                and normalize_website_domain(session.website_url) == normalized_domain
            ),
            None,
        )

    async def get_business_profile_by_website_domain(
        self,
        website_domain: str,
    ) -> BusinessProfileRecord | None:
        normalized_domain = normalize_website_domain(website_domain)
        return next(
            (
                profile
                for profile in self.business_profiles.values()
                if normalize_website_domain(profile.website_url) == normalized_domain
            ),
            None,
        )

    async def get_session(self, session_id: UUID) -> OnboardingSessionRecord | None:
        return self.sessions.get(session_id)

    async def update_session(
        self,
        session_id: UUID,
        update: OnboardingSessionUpdate,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        updates = {
            field: getattr(update, field)
            for field in update.model_fields_set
        }
        next_values = {
            **updates,
            "updated_at": datetime.now(timezone.utc),
        }
        if update.current_step:
            next_values["current_step"] = update.current_step
        updated = session.model_copy(update=next_values)
        self.sessions[session_id] = updated
        return updated

    async def save_session_analysis(
        self,
        session_id: UUID,
        *,
        analysis: WebsiteAnalysisResult,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        updated = session.model_copy(
            update={
                "status": "ready_for_review",
                "current_step": "analysis",
                "analysis": analysis,
                "business_profile": analysis.business_profile,
                "business_summary": analysis.business_summary,
                "contact_info": analysis.contact_info,
                "knowledge_sources": analysis.knowledge_sources,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def save_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        self.session_username_email_token_hashes[session_id] = token_hash
        self.session_username_email_token_used_at.pop(session_id, None)
        updated = session.model_copy(
            update={
                "status": "username_email_verification_pending",
                "current_step": "username-email-verification",
                "username_email_verified": False,
                "username_email_verification_expires_at": expires_at,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def consume_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool:
        session = self._require_session(session_id)
        if session.username_email_verification_expires_at is None:
            return False
        if session.username_email_verification_expires_at <= datetime.now(timezone.utc):
            return False
        if self.session_username_email_token_used_at.get(session_id):
            return False
        if self.session_username_email_token_hashes.get(session_id) != token_hash:
            return False
        self.session_username_email_token_used_at[session_id] = datetime.now(timezone.utc)
        updated = session.model_copy(
            update={
                "status": "draft",
                "current_step": "website",
                "username_email_verified": True,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return True

    async def inspect_username_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> OnboardingEmailVerificationDiagnostic:
        session = self._require_session(session_id)
        stored_token_hash = self.session_username_email_token_hashes.get(session_id)
        used_at = self.session_username_email_token_used_at.get(session_id)
        expires_at = session.username_email_verification_expires_at
        now = datetime.now(timezone.utc)
        return OnboardingEmailVerificationDiagnostic(
            admin_email_verified=session.username_email_verified,
            has_token_hash=stored_token_hash is not None,
            token_matches=stored_token_hash == token_hash,
            token_used=used_at is not None,
            token_expired=expires_at is not None and expires_at <= now,
            expires_at=expires_at,
            used_at=used_at,
            submitted_token_fingerprint=token_fingerprint(token_hash),
            stored_token_fingerprint=token_fingerprint(stored_token_hash),
        )

    async def save_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        self.session_website_email_token_hashes[session_id] = token_hash
        self.session_website_email_token_used_at.pop(session_id, None)
        updated = session.model_copy(
            update={
                "status": "website_verification_pending",
                "current_step": "website-email-verification",
                "website_email_verified": False,
                "website_email_verification_expires_at": expires_at,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def consume_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool:
        session = self._require_session(session_id)
        if session.website_email_verification_expires_at is None:
            return False
        if session.website_email_verification_expires_at <= datetime.now(timezone.utc):
            return False
        if self.session_website_email_token_used_at.get(session_id):
            return False
        if self.session_website_email_token_hashes.get(session_id) != token_hash:
            return False
        self.session_website_email_token_used_at[session_id] = datetime.now(timezone.utc)
        updated = session.model_copy(
            update={
                "status": "draft",
                "current_step": "analyzing",
                "website_email_verified": True,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return True

    async def inspect_website_email_verification_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> OnboardingEmailVerificationDiagnostic:
        session = self._require_session(session_id)
        stored_token_hash = self.session_website_email_token_hashes.get(session_id)
        used_at = self.session_website_email_token_used_at.get(session_id)
        expires_at = session.website_email_verification_expires_at
        now = datetime.now(timezone.utc)
        return OnboardingEmailVerificationDiagnostic(
            admin_email_verified=session.website_email_verified,
            has_token_hash=stored_token_hash is not None,
            token_matches=stored_token_hash == token_hash,
            token_used=used_at is not None,
            token_expired=expires_at is not None and expires_at <= now,
            expires_at=expires_at,
            used_at=used_at,
            submitted_token_fingerprint=token_fingerprint(token_hash),
            stored_token_fingerprint=token_fingerprint(stored_token_hash),
        )

    async def save_telegram_setup_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
        setup_url: str,
        expires_at: datetime,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        self.session_token_hashes[session_id] = token_hash
        self.session_token_used_at.pop(session_id, None)
        updated = session.model_copy(
            update={
                "status": "awaiting_telegram_setup",
                "current_step": "telegram-setup",
                "telegram_setup_url": setup_url,
                "telegram_setup_token_expires_at": expires_at,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def consume_telegram_setup_token(
        self,
        session_id: UUID,
        *,
        token_hash: str,
    ) -> bool:
        session = self._require_session(session_id)
        if session.telegram_setup_token_expires_at is None:
            return False
        if session.telegram_setup_token_expires_at <= datetime.now(timezone.utc):
            return False
        if self.session_token_used_at.get(session_id):
            return False
        if self.session_token_hashes.get(session_id) != token_hash:
            return False
        self.session_token_used_at[session_id] = datetime.now(timezone.utc)
        return True

    async def save_telegram_setup(
        self,
        session_id: UUID,
        telegram: OnboardingTelegramSetup,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        updated = session.model_copy(
            update={
                "status": "ready_to_submit",
                "current_step": "submit",
                "telegram": telegram,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def save_provider_projects(
        self,
        session_id: UUID,
        provider_projects: OnboardingProviderProjects,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        updated = session.model_copy(
            update={
                "provider_projects": provider_projects,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def mark_session_submitted(
        self,
        session_id: UUID,
        *,
        job_id: UUID,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        updated = session.model_copy(
            update={
                "status": "submitted",
                "current_step": "complete",
                "submitted_job_id": job_id,
                "error": None,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def mark_session_failed(
        self,
        session_id: UUID,
        *,
        error: str,
    ) -> OnboardingSessionRecord:
        session = self._require_session(session_id)
        updated = session.model_copy(
            update={
                "status": "failed",
                "error": error,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.sessions[session_id] = updated
        return updated

    async def _update_job(
        self,
        job_id: UUID,
        **updates,
    ) -> OnboardingJobRecord:
        job = self.jobs[job_id]
        updated = job.model_copy(
            update={
                **updates,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.jobs[job_id] = updated
        return updated

    def _require_session(self, session_id: UUID) -> OnboardingSessionRecord:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"Onboarding session not found: {session_id}")
        return session


class ExtractiveAnswerGenerator:
    async def generate(
        self,
        query: str,
        documents: list[Document],
        conversation_history: list[StoredMessage] | None = None,
        conversation_metadata: ConversationPromptMetadata | None = None,
        tenant_config: TenantConfig | None = None,
    ) -> AnswerGenerationResult:
        if not documents:
            return AnswerGenerationResult(
                answer="I could not find enough information to answer that safely.",
                confidence=0.0,
                answer_found=False,
                grounded=False,
            )
        source = documents[0]
        answer = source.page_content
        overlap = len(tokenize(query) & tokenize(answer)) / max(len(tokenize(query)), 1)
        confidence = min(0.95, 0.55 + overlap)
        return AnswerGenerationResult(
            answer=answer,
            confidence=confidence,
            answer_found=True,
            grounded=True,
        )

class RuleBasedQuestionPlanner:
    human_request_phrases = (
        "human agent",
        "human support",
        "real person",
        "talk to someone",
        "speak to someone",
        "support team member",
        "customer service agent",
        "manager",
    )
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
        explicit_human_request = any(
            phrase in text for phrase in self.human_request_phrases
        )
        if self._is_arithmetic_question(text) or any(
            phrase in text for phrase in self.out_of_scope_phrases
        ):
            return QuestionPlan(
                in_scope=False,
                needs_conversation_history=False,
                explicit_human_request=explicit_human_request,
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
            explicit_human_request=explicit_human_request,
            explanation="local heuristic planner",
        )

    def _is_arithmetic_question(self, text: str) -> bool:
        arithmetic_expression = r"[\d\s.,()+\-*/x×÷=%?]+"
        if re.fullmatch(arithmetic_expression, text):
            return bool(re.search(r"\d", text) and re.search(r"[+\-*/x×÷=%]", text))

        math_prefix = r"(what\s+is|what's|calculate|compute|solve)"
        return bool(re.fullmatch(rf"{math_prefix}\s+{arithmetic_expression}", text))
