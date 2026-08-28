import logging
import secrets
from uuid import UUID

import httpx
from langchain_core.documents import Document

from app.adapters.telegram import TelegramSecretWriter, TelegramWebhookRegistrar
from app.knowledge import chunk_text
from app.models import (
    OnboardingContactPoint,
    OnboardingJobCreate,
    OnboardingJobRecord,
    OnboardingJobRetryRequest,
    TenantConfig,
    WebsiteResearchSource,
)
from app.notifications import EmailSender
from app.ports import (
    OnboardingRepository,
    ProviderProjectProvisioner,
    RetrievalStore,
    TenantConfigRepository,
    TenantRepository,
)
from app.provider_projects import MetadataOnlyProviderProjectProvisioner
from app.tenancy import tenant_slug

logger = logging.getLogger(__name__)


class OnboardingJobService:
    def __init__(
        self,
        *,
        onboarding: OnboardingRepository,
        tenants: TenantRepository,
        tenant_configs: TenantConfigRepository,
        retrieval: RetrievalStore | None = None,
        provider_project_provisioner: ProviderProjectProvisioner | None = None,
        telegram_secret_writer: TelegramSecretWriter | None = None,
        telegram_webhook_registrar: TelegramWebhookRegistrar | None = None,
        email_sender: EmailSender | None = None,
        onboarding_review_email: str | None = None,
    ) -> None:
        self.onboarding = onboarding
        self.tenants = tenants
        self.tenant_configs = tenant_configs
        self.retrieval = retrieval
        self.provider_project_provisioner = (
            provider_project_provisioner or MetadataOnlyProviderProjectProvisioner()
        )
        self.telegram_secret_writer = telegram_secret_writer
        self.telegram_webhook_registrar = telegram_webhook_registrar
        self.email_sender = email_sender
        self.onboarding_review_email = onboarding_review_email

    async def start_job(self, request: OnboardingJobCreate) -> OnboardingJobRecord:
        job = await self.onboarding.create_job(
            idempotency_key=request.idempotency_key,
            request_payload=sanitized_job_payload(request),
        )
        logger.info(
            "Accepted onboarding job job_id=%s idempotency_key=%s business_name=%s",
            job.job_id,
            job.idempotency_key,
            request.business_profile.business_name,
        )
        return job

    async def get_job(self, job_id: UUID) -> OnboardingJobRecord | None:
        return await self.onboarding.get_job(job_id)

    async def retry_job(
        self,
        job_id: UUID,
        request: OnboardingJobRetryRequest,
    ) -> tuple[OnboardingJobRecord, OnboardingJobCreate]:
        job = await self.onboarding.get_job(job_id)
        if job is None:
            raise KeyError(f"Onboarding job not found: {job_id}")
        if job.status != "failed":
            raise ValueError("Only failed onboarding jobs can be retried")

        persisted_payload = await self.onboarding.get_job_payload(job_id)
        if persisted_payload is None:
            raise ValueError("Failed onboarding job payload is no longer available")

        retry_payload = {
            **persisted_payload,
            "telegram": {
                "bot_token": request.telegram.bot_token,
                "webhook_secret_token": secrets.token_hex(32),
            },
        }
        retry_request = OnboardingJobCreate.model_validate(retry_payload)
        if job.idempotency_key != retry_request.idempotency_key:
            raise ValueError("Stored onboarding job payload idempotency_key is invalid")

        retried_job = await self.onboarding.mark_job_accepted(job_id)
        return retried_job, retry_request

    async def process_job(self, job_id: UUID, request: OnboardingJobCreate) -> None:
        logger.info(
            "Starting onboarding job processing job_id=%s idempotency_key=%s "
            "business_name=%s provisioner=%s",
            job_id,
            request.idempotency_key,
            request.business_profile.business_name,
            type(self.provider_project_provisioner).__name__,
        )
        job = await self.onboarding.get_job(job_id)
        if job is None:
            logger.warning("Onboarding job disappeared before processing job_id=%s", job_id)
            return
        if job.status in {"running", "succeeded"}:
            logger.info(
                "Skipping onboarding job processing because job is already terminal/running "
                "job_id=%s status=%s",
                job_id,
                job.status,
            )
            return

        tenant = None
        try:
            await self.onboarding.mark_job_running(job_id)
            logger.info("Marked onboarding job running job_id=%s", job_id)
            slug = tenant_slug(request.business_profile.business_name)
            existing_tenant = await self.tenants.get_by_slug(slug)
            if existing_tenant is not None and job.tenant_id != existing_tenant.tenant_id:
                raise ValueError("Tenant with matching name or slug already exists")

            logger.info(
                "Provisioning provider projects for onboarding job job_id=%s tenant_slug=%s",
                job_id,
                slug,
            )
            provider_projects = await self.provider_project_provisioner.provision_for(
                business_name=request.business_profile.business_name,
                website_url=str(request.business_profile.website_url),
                provider_projects=request.provider_projects,
                session_id=onboarding_session_id_from_request(request),
            )
            logger.info(
                "Provider project provisioning completed for onboarding job job_id=%s "
                "llm_project_id=%s llm_project_name=%s langsmith_project=%s "
                "web_search_provider=%s web_search_project_name=%s",
                job_id,
                provider_projects.llm_project_id,
                provider_projects.llm_project_name,
                provider_projects.langsmith_project,
                provider_projects.web_search_provider,
                provider_projects.web_search_project_name,
            )
            tenant = existing_tenant or await self.tenants.create(
                display_name=request.business_profile.business_name,
                slug=slug,
                selected_plan=request.selected_plan,
            )
            tenant_config = await self.tenant_configs.upsert(
                tenant.tenant_id,
                selected_plan=request.selected_plan,
                enabled_features=["telegram"],
                answer_prompt_instructions=request.answer_prompt_instructions,
                llm_project_id=provider_projects.llm_project_id,
                llm_project_name=provider_projects.llm_project_name,
                langsmith_project=provider_projects.langsmith_project,
                telegram_secret_name=telegram_secret_name_for_request(request),
                web_search_provider=provider_projects.web_search_provider,
                web_search_project_name=provider_projects.web_search_project_name,
            )
            await self.onboarding.save_business_profile(
                tenant.tenant_id,
                request.business_profile,
            )
            await self.onboarding.replace_contact_points(
                tenant.tenant_id,
                contact_points_from_request(request),
            )
            await self.onboarding.save_owner_membership(
                tenant.tenant_id,
                request.admin,
            )
            await self.create_initial_knowledge_documents(
                request,
                tenant_config=tenant_config,
                onboarding_session_id=onboarding_session_id_from_request(request),
            )
            await self.write_telegram_secret(request)
            await self.register_telegram_webhook(
                request,
                tenant_id=tenant.tenant_id,
            )
            await self.onboarding.mark_job_succeeded(
                job_id,
                tenant_id=tenant.tenant_id,
                tenant_slug=tenant.slug,
            )
            await self.send_success_email(
                request,
                tenant_id=tenant.tenant_id,
                tenant_slug=tenant.slug,
            )
            await post_job_callback(
                request,
                {
                    "event": "onboarding_job_succeeded",
                    "job_id": str(job_id),
                    "tenant_id": tenant.tenant_id,
                    "tenant_slug": tenant.slug,
                    "admin": request.admin.model_dump(mode="json"),
                    "business_profile": request.business_profile.model_dump(mode="json"),
                },
            )
        except Exception as error:
            logger.exception("Onboarding job failed job_id=%s", job_id)
            await self.onboarding.mark_job_failed(
                job_id,
                error=str(error),
                tenant_id=tenant.tenant_id if tenant else None,
                tenant_slug=tenant.slug if tenant else None,
            )
            await self.send_failure_email(request, job_id=job_id, error=str(error))
            await post_job_callback(
                request,
                {
                    "event": "onboarding_job_failed",
                    "job_id": str(job_id),
                    "error": str(error),
                    "admin": request.admin.model_dump(mode="json"),
                    "business_profile": request.business_profile.model_dump(mode="json"),
                },
            )

    async def create_initial_knowledge_documents(
        self,
        request: OnboardingJobCreate,
        *,
        tenant_config: TenantConfig,
        onboarding_session_id: str | None,
    ) -> None:
        if self.retrieval is None:
            logger.info("Skipping onboarding KB creation because no retrieval store is configured")
            return
        namespace = tenant_config.vector_namespace
        if not namespace:
            logger.info(
                "Skipping onboarding KB creation because tenant has no vector namespace "
                "tenant_id=%s",
                tenant_config.tenant_id,
            )
            return
        if onboarding_session_id:
            logger.info(
                "Removing existing onboarding KB documents before recreation "
                "tenant_id=%s namespace=%s onboarding_session_id=%s",
                tenant_config.tenant_id,
                namespace,
                onboarding_session_id,
            )
            await self.retrieval.delete_by_metadata(
                namespace,
                "onboarding_session_id",
                onboarding_session_id,
            )
        documents = onboarding_knowledge_documents(
            request,
            tenant_id=tenant_config.tenant_id,
            onboarding_session_id=onboarding_session_id,
        )
        if not documents:
            logger.info(
                "No onboarding KB documents were generated tenant_id=%s namespace=%s",
                tenant_config.tenant_id,
                namespace,
            )
            return
        logger.info(
            "Creating onboarding KB documents tenant_id=%s namespace=%s document_count=%s",
            tenant_config.tenant_id,
            namespace,
            len(documents),
        )
        await self.retrieval.upsert(documents, namespace)

    async def write_telegram_secret(self, request: OnboardingJobCreate) -> None:
        if self.telegram_secret_writer is None:
            logger.info(
                "Skipping tenant Telegram Secret creation because no writer is configured "
                "secret_name=%s",
                telegram_secret_name_for_request(request),
            )
            return
        secret_name = telegram_secret_name_for_request(request)
        await self.telegram_secret_writer.write_secret(
            secret_name=secret_name,
            bot_token=request.telegram.bot_token,
            webhook_secret_token=request.telegram.webhook_secret_token,
        )

    async def register_telegram_webhook(
        self,
        request: OnboardingJobCreate,
        *,
        tenant_id: str,
    ) -> None:
        if self.telegram_webhook_registrar is None:
            logger.info(
                "Skipping Telegram webhook registration because no registrar is configured "
                "tenant_id=%s",
                tenant_id,
            )
            return
        await self.telegram_webhook_registrar.register_webhook(
            tenant_id=tenant_id,
            bot_token=request.telegram.bot_token,
            webhook_secret_token=request.telegram.webhook_secret_token,
        )

    async def send_success_email(
        self,
        request: OnboardingJobCreate,
        *,
        tenant_id: str,
        tenant_slug: str,
    ) -> None:
        if self.email_sender is None:
            return
        recipients = dedupe_recipients(
            [str(request.admin.email), self.onboarding_review_email]
        )
        if not recipients:
            return
        await self.email_sender.send_email(
            to=recipients,
            subject="Customer-service onboarding completed",
            text=(
                f"Hello {request.admin.name},\n\n"
                f"Customer-service onboarding for "
                f"{request.business_profile.business_name} is complete.\n\n"
                f"Tenant ID: {tenant_id}\n"
                f"Tenant slug: {tenant_slug}\n"
            ),
        )

    async def send_failure_email(
        self,
        request: OnboardingJobCreate,
        *,
        job_id: UUID,
        error: str,
    ) -> None:
        if self.email_sender is None or not self.onboarding_review_email:
            return
        await self.email_sender.send_email(
            to=[self.onboarding_review_email],
            subject="Customer-service onboarding failed",
            text=(
                f"Onboarding failed for {request.business_profile.business_name}.\n\n"
                f"Job: {job_id}\n"
                f"Admin: {request.admin.name} <{request.admin.email}>\n"
                f"Error: {error}\n"
            ),
        )


def contact_points_from_request(
    request: OnboardingJobCreate,
) -> list[OnboardingContactPoint]:
    contact_points = [
        OnboardingContactPoint(
            kind="phone",
            label="Business phone",
            value=request.business_profile.business_phone,
            is_primary=True,
        ),
        OnboardingContactPoint(
            kind="email",
            label="Business email",
            value=request.business_profile.business_email,
            is_primary=True,
        ),
        OnboardingContactPoint(
            kind="website",
            label="Website",
            url=request.business_profile.website_url,
            is_primary=True,
        ),
    ]
    if request.business_profile.google_place_url:
        contact_points.append(
            OnboardingContactPoint(
                kind="map",
                label="Google place",
                url=request.business_profile.google_place_url,
            )
        )
    contact_points.extend(request.contact_info)
    return contact_points


def onboarding_knowledge_documents(
    request: OnboardingJobCreate,
    *,
    tenant_id: str,
    onboarding_session_id: str | None,
) -> list[Document]:
    documents = []
    documents.extend(
        chunk_knowledge_document(
            onboarding_profile_markdown(request),
            source="onboarding:approved-profile",
            source_url=str(request.business_profile.website_url),
            metadata={
                "tenant_id": tenant_id,
                "source_type": "onboarding",
                "provider": "customer-service",
                "onboarding_session_id": onboarding_session_id or "",
            },
        )
    )
    for source in request.knowledge_sources:
        documents.extend(
            chunk_website_source(
                source,
                tenant_id=tenant_id,
                onboarding_session_id=onboarding_session_id,
            )
        )
    return documents


def onboarding_profile_markdown(request: OnboardingJobCreate) -> str:
    contact_lines = []
    for point in contact_points_from_request(request):
        value = point.url or point.value or ""
        label = f" ({point.label})" if point.label else ""
        if value:
            contact_lines.append(f"- {point.kind}{label}: {value}")

    return "\n".join(
        [
            f"# {request.business_profile.business_name}",
            "",
            "## Business profile",
            f"- Website: {request.business_profile.website_url}",
            f"- Location name: {request.business_profile.location_name}",
            f"- Physical location: {request.business_profile.physical_location}",
            f"- Business phone: {request.business_profile.business_phone}",
            f"- Business email: {request.business_profile.business_email}",
            (
                f"- Google place URL: {request.business_profile.google_place_url}"
                if request.business_profile.google_place_url
                else "- Google place URL:"
            ),
            "",
            "## Assistant profile",
            f"- Agent name: {request.agent_name}",
            f"- Agent description: {request.agent_description}",
            "",
            "## Contact information",
            *(contact_lines or ["- No contact information was approved."]),
            "",
            "## Answer instructions",
            request.answer_prompt_instructions,
        ]
    )


def chunk_website_source(
    source: WebsiteResearchSource,
    *,
    tenant_id: str,
    onboarding_session_id: str | None,
) -> list[Document]:
    text = source.text.strip()
    if not text:
        return []
    return chunk_knowledge_document(
        text,
        source=f"website:{source.url}",
        source_url=source.url,
        metadata={
            "tenant_id": tenant_id,
            "source_type": "website",
            "provider": source.provider,
            "title": source.title or "",
            "retrieved_at": source.retrieved_at.isoformat(),
            "onboarding_session_id": onboarding_session_id or "",
        },
    )


def chunk_knowledge_document(
    text: str,
    *,
    source: str,
    source_url: str | None,
    metadata: dict,
) -> list[Document]:
    documents = []
    for index, chunk in enumerate(chunk_text(text)):
        chunk_id = f"{source}#{index:04d}"
        documents.append(
            Document(
                page_content=chunk,
                metadata={
                    **metadata,
                    "source": source,
                    "source_url": source_url,
                    "chunk_id": chunk_id,
                },
            )
        )
    return documents


def onboarding_session_id_from_request(request: OnboardingJobCreate) -> str | None:
    prefix = "onboarding-session-"
    if request.idempotency_key.startswith(prefix):
        return request.idempotency_key.removeprefix(prefix)
    return None


def sanitized_job_payload(request: OnboardingJobCreate) -> dict:
    payload = request.model_dump(mode="json")
    payload["telegram"] = {
        "secret_name": telegram_secret_name_for_request(request),
        "bot_token_received": True,
        "webhook_secret_token_received": True,
    }
    return payload


def telegram_secret_name_for_request(request: OnboardingJobCreate) -> str:
    return f"tenant-{tenant_slug(request.business_profile.business_name)}-telegram"


def dedupe_recipients(recipients: list[str | None]) -> list[str]:
    seen = set()
    deduped = []
    for recipient in recipients:
        if not recipient:
            continue
        normalized = recipient.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(recipient.strip())
    return deduped


async def post_job_callback(request: OnboardingJobCreate, payload: dict) -> None:
    if not request.callback_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(str(request.callback_url), json=payload)
            response.raise_for_status()
    except Exception:
        logger.exception("Failed to post onboarding job callback")
