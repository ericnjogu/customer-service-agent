import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID

from app.config import Settings
from app.models import (
    ONBOARDING_TERMS_VERSION,
    OnboardingEmailVerificationDiagnostic,
    OnboardingEmailVerificationRequest,
    OnboardingJobCreate,
    OnboardingJobRecord,
    OnboardingProviderProjects,
    OnboardingSessionCreate,
    OnboardingSessionRecord,
    OnboardingSessionUpdate,
    OnboardingSessionWebsiteRequest,
    OnboardingTelegramSetup,
    OnboardingTelegramSetupRequest,
)
from app.notifications import EmailSender
from app.onboarding_jobs import OnboardingJobService
from app.ports import (
    OnboardingRepository,
    TenantRepository,
    WebsiteAnalyzer,
)
from app.tenancy import DEFAULT_TENANT_PLAN, tenant_slug

logger = logging.getLogger(__name__)


class OnboardingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class OnboardingSessionSubmission:
    job: OnboardingJobRecord
    request: OnboardingJobCreate


@dataclass(frozen=True)
class OnboardingTelegramSetupResult:
    session: OnboardingSessionRecord
    submission: OnboardingSessionSubmission | None = None


class OnboardingSessionService:
    def __init__(
        self,
        *,
        onboarding: OnboardingRepository,
        onboarding_jobs: OnboardingJobService,
        tenants: TenantRepository,
        email_sender: EmailSender,
        website_analyzer: WebsiteAnalyzer,
        settings: Settings,
    ) -> None:
        self.onboarding = onboarding
        self.onboarding_jobs = onboarding_jobs
        self.tenants = tenants
        self.email_sender = email_sender
        self.website_analyzer = website_analyzer
        self.settings = settings

    async def create_session(
        self,
        request: OnboardingSessionCreate,
    ) -> OnboardingSessionRecord:
        started_at = time.perf_counter()
        terms_accepted_at = datetime.now(timezone.utc)
        logger.info(
            "Creating onboarding session after account validation username_email=%s",
            request.admin.username_email,
        )
        session = await self.onboarding.create_session(
            admin=request.admin,
            terms_version=ONBOARDING_TERMS_VERSION,
            terms_accepted_at=terms_accepted_at,
        )
        logger.info(
            "Onboarding session persisted before username verification email "
            "session_id=%s username_email=%s elapsed_seconds=%.3f",
            session.session_id,
            session.admin.username_email,
            time.perf_counter() - started_at,
        )
        return await self.send_username_email_verification(session.session_id)

    async def save_website(
        self,
        session_id: UUID,
        request: OnboardingSessionWebsiteRequest,
    ) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        if not session.username_email_verified:
            raise OnboardingValidationError(
                "Username email must be verified before website verification"
            )
        validate_website_verification_fields(
            str(request.website_url),
            str(request.website_verification_email),
            require_email_domain_match=(
                self.settings.onboarding_require_admin_email_domain_match
            ),
        )
        await self._validate_no_duplicate_website(
            str(request.website_url),
            session_id=session_id,
        )
        updated = await self.onboarding.save_session_website(
            session_id,
            website_url=str(request.website_url),
            website_verification_email=str(request.website_verification_email),
        )
        return await self.send_website_email_verification(updated.session_id)

    async def get_session(self, session_id: UUID) -> OnboardingSessionRecord | None:
        return await self.onboarding.get_session(session_id)

    async def update_session(
        self,
        session_id: UUID,
        update: OnboardingSessionUpdate,
    ) -> OnboardingSessionRecord:
        await self._require_session(session_id)
        return await self.onboarding.update_session(session_id, update)

    async def analyze_website(self, session_id: UUID) -> OnboardingSessionRecord:
        started_at = time.perf_counter()
        session = await self._require_session(session_id)
        logger.info(
            "Starting onboarding website analysis session_id=%s website_url=%s "
            "username_email_verified=%s website_email_verified=%s analyzer=llm",
            session.session_id,
            session.website_url,
            session.username_email_verified,
            session.website_email_verified,
        )
        if not session.username_email_verified or not session.website_email_verified:
            logger.info(
                "Rejected onboarding website analysis because required emails are not "
                "verified session_id=%s elapsed_seconds=%.3f",
                session.session_id,
                time.perf_counter() - started_at,
            )
            raise OnboardingValidationError(
                "Username and website emails must be verified before analysis"
            )
        if not session.website_url:
            raise OnboardingValidationError(
                "Website URL must be saved before analysis"
            )
        try:
            analysis = await self.website_analyzer.analyze(session)
        except Exception as error:
            logger.exception(
                "Onboarding website LLM analysis failed session_id=%s website_url=%s "
                "elapsed_seconds=%.3f",
                session.session_id,
                session.website_url,
                time.perf_counter() - started_at,
            )
            raise RuntimeError("onboarding website analysis failed") from error
        logger.info(
            "Generated onboarding website analysis session_id=%s business_name=%s "
            "business_summary_chars=%s contact_info=%s analyzer=llm elapsed_seconds=%.3f",
            session.session_id,
            analysis.business_profile.business_name,
            len(analysis.business_summary),
            len(analysis.contact_info),
            time.perf_counter() - started_at,
        )
        updated = await self.onboarding.save_session_analysis(
            session_id,
            analysis=analysis,
        )
        logger.info(
            "Saved onboarding website analysis session_id=%s status=%s "
            "current_step=%s elapsed_seconds=%.3f",
            updated.session_id,
            updated.status,
            updated.current_step,
            time.perf_counter() - started_at,
        )
        return updated

    async def send_username_email_verification(
        self,
        session_id: UUID,
    ) -> OnboardingSessionRecord:
        started_at = time.perf_counter()
        session = await self._require_session(session_id)
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=self.settings.onboarding_email_verification_token_ttl_minutes
        )
        verify_url = username_email_verification_url(
            self.settings.web_public_base_url,
            session.session_id,
            token,
        )
        resume_url = onboarding_resume_url(
            self.settings.web_public_base_url,
            session.session_id,
        )
        updated = await self.onboarding.save_username_email_verification_token(
            session_id,
            token_hash=token_hash(token),
            expires_at=expires_at,
        )
        logger.info(
            "Saved onboarding username verification token session_id=%s "
            "username_email=%s expires_at=%s",
            session.session_id,
            session.admin.username_email,
            expires_at.isoformat(),
        )
        logger.info(
            "Sending onboarding username email verification session_id=%s "
            "username_email=%s "
            "web_public_base_url=%s expires_at=%s",
            session.session_id,
            session.admin.username_email,
            self.settings.web_public_base_url,
            expires_at.isoformat(),
        )
        try:
            await self.email_sender.send_email(
                to=[str(session.admin.username_email)],
                subject="Verify your customer-service onboarding account email",
                text=(
                    f"Hello {session.admin.name},\n\n"
                    "Please verify the email address you will use for your future "
                    "customer-service dashboard account.\n\n"
                    f"{verify_url}\n\n"
                    "You can resume this onboarding later from:\n\n"
                    f"{resume_url}\n\n"
                    f"This link expires in "
                    f"{self.settings.onboarding_email_verification_token_ttl_minutes} "
                    "minutes."
                ),
            )
        except Exception:
            logger.exception(
                "Onboarding username email verification failed session_id=%s "
                "username_email=%s "
                "elapsed_seconds=%.3f",
                session.session_id,
                session.admin.username_email,
                time.perf_counter() - started_at,
            )
            raise
        logger.info(
            "Onboarding username email verification queued session_id=%s "
            "username_email=%s "
            "elapsed_seconds=%.3f",
            session.session_id,
            session.admin.username_email,
            time.perf_counter() - started_at,
        )
        return updated

    async def send_website_email_verification(
        self,
        session_id: UUID,
    ) -> OnboardingSessionRecord:
        started_at = time.perf_counter()
        session = await self._require_session(session_id)
        if not session.website_verification_email:
            raise OnboardingValidationError("Website verification email is required")
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=self.settings.onboarding_email_verification_token_ttl_minutes
        )
        verify_url = website_email_verification_url(
            self.settings.web_public_base_url,
            session.session_id,
            token,
        )
        resume_url = onboarding_resume_url(
            self.settings.web_public_base_url,
            session.session_id,
        )
        updated = await self.onboarding.save_website_email_verification_token(
            session_id,
            token_hash=token_hash(token),
            expires_at=expires_at,
        )
        logger.info(
            "Sending onboarding website email verification session_id=%s "
            "website_verification_email=%s web_public_base_url=%s expires_at=%s",
            session.session_id,
            session.website_verification_email,
            self.settings.web_public_base_url,
            expires_at.isoformat(),
        )
        try:
            await self.email_sender.send_email(
                to=[str(session.website_verification_email)],
                subject="Verify your business website for customer-service onboarding",
                text=(
                    f"Hello {session.admin.name},\n\n"
                    "Please verify this business website contact email to continue "
                    "customer-service onboarding.\n\n"
                    f"{verify_url}\n\n"
                    "You can resume this onboarding later from:\n\n"
                    f"{resume_url}\n\n"
                    f"This link expires in "
                    f"{self.settings.onboarding_email_verification_token_ttl_minutes} "
                    "minutes."
                ),
            )
        except Exception:
            logger.exception(
                "Onboarding website email verification failed session_id=%s "
                "website_verification_email=%s elapsed_seconds=%.3f",
                session.session_id,
                session.website_verification_email,
                time.perf_counter() - started_at,
            )
            raise
        logger.info(
            "Onboarding website email verification queued session_id=%s "
            "website_verification_email=%s elapsed_seconds=%.3f",
            session.session_id,
            session.website_verification_email,
            time.perf_counter() - started_at,
        )
        return updated

    async def verify_username_email(
        self,
        session_id: UUID,
        request: OnboardingEmailVerificationRequest,
    ) -> OnboardingSessionRecord:
        await self._require_session(session_id)
        submitted_token_hash = token_hash(request.token)
        accepted = await self.onboarding.consume_username_email_verification_token(
            session_id,
            token_hash=submitted_token_hash,
        )
        if not accepted:
            diagnostic = await self.onboarding.inspect_username_email_verification_token(
                session_id,
                token_hash=submitted_token_hash,
            )
            logger.warning(
                "Username email verification rejected session_id=%s reason=%s "
                "email_verified=%s has_token_hash=%s token_matches=%s "
                "token_used=%s token_expired=%s expires_at=%s used_at=%s "
                "submitted_token_fingerprint=%s stored_token_fingerprint=%s",
                session_id,
                email_verification_rejection_reason(diagnostic),
                diagnostic.admin_email_verified,
                diagnostic.has_token_hash,
                diagnostic.token_matches,
                diagnostic.token_used,
                diagnostic.token_expired,
                diagnostic.expires_at.isoformat() if diagnostic.expires_at else None,
                diagnostic.used_at.isoformat() if diagnostic.used_at else None,
                diagnostic.submitted_token_fingerprint,
                diagnostic.stored_token_fingerprint,
            )
            raise OnboardingValidationError(
                "Email verification link is missing, expired, invalid, or already used"
            )
        session = await self._require_session(session_id)
        return session

    async def verify_website_email(
        self,
        session_id: UUID,
        request: OnboardingEmailVerificationRequest,
    ) -> OnboardingSessionRecord:
        await self._require_session(session_id)
        submitted_token_hash = token_hash(request.token)
        accepted = await self.onboarding.consume_website_email_verification_token(
            session_id,
            token_hash=submitted_token_hash,
        )
        if not accepted:
            diagnostic = await self.onboarding.inspect_website_email_verification_token(
                session_id,
                token_hash=submitted_token_hash,
            )
            logger.warning(
                "Website email verification rejected session_id=%s reason=%s "
                "email_verified=%s has_token_hash=%s token_matches=%s "
                "token_used=%s token_expired=%s expires_at=%s used_at=%s "
                "submitted_token_fingerprint=%s stored_token_fingerprint=%s",
                session_id,
                email_verification_rejection_reason(diagnostic),
                diagnostic.admin_email_verified,
                diagnostic.has_token_hash,
                diagnostic.token_matches,
                diagnostic.token_used,
                diagnostic.token_expired,
                diagnostic.expires_at.isoformat() if diagnostic.expires_at else None,
                diagnostic.used_at.isoformat() if diagnostic.used_at else None,
                diagnostic.submitted_token_fingerprint,
                diagnostic.stored_token_fingerprint,
            )
            raise OnboardingValidationError(
                "Email verification link is missing, expired, invalid, or already used"
            )
        session = await self._require_session(session_id)
        return session

    async def prepare_telegram_setup(self, session_id: UUID) -> OnboardingSessionRecord:
        session = await self._require_session(session_id)
        if not session.username_email_verified or not session.website_email_verified:
            raise OnboardingValidationError(
                "Username and website emails must be verified before Telegram setup"
            )
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=self.settings.onboarding_action_token_ttl_minutes
        )
        setup_url = telegram_setup_url(
            self.settings.web_public_base_url,
            session.session_id,
            token,
        )
        updated = await self.onboarding.save_telegram_setup_token(
            session_id,
            token_hash=token_hash(token),
            setup_url=setup_url,
            expires_at=expires_at,
        )
        await self._send_telegram_setup_email(session, setup_url)
        return updated

    async def save_telegram_setup(
        self,
        session_id: UUID,
        request: OnboardingTelegramSetupRequest,
    ) -> OnboardingTelegramSetupResult:
        await self._require_session(session_id)
        accepted = await self.onboarding.consume_telegram_setup_token(
            session_id,
            token_hash=token_hash(request.token),
        )
        if not accepted:
            raise OnboardingValidationError(
                "Telegram setup link is missing, expired, invalid, or already used"
            )
        await self.onboarding.save_telegram_setup(
            session_id,
            OnboardingTelegramSetup(
                bot_token=request.bot_token,
                webhook_secret_token=secrets.token_hex(32),
            ),
        )
        logger.info("Telegram setup accepted session_id=%s", session_id)
        submission = await self.submit_session(session_id)
        logger.info(
            "Submitted onboarding session after Telegram setup session_id=%s "
            "job_id=%s job_status=%s",
            session_id,
            submission.job.job_id,
            submission.job.status,
        )
        submitted = await self._require_session(session_id)
        return OnboardingTelegramSetupResult(
            session=submitted,
            submission=submission,
        )

    async def save_provider_projects(
        self,
        session_id: UUID,
        provider_projects: OnboardingProviderProjects,
    ) -> OnboardingSessionRecord:
        await self._require_session(session_id)
        return await self.onboarding.save_provider_projects(
            session_id,
            provider_projects,
        )

    async def submit_session(self, session_id: UUID) -> OnboardingSessionSubmission:
        session = await self._require_session(session_id)
        if not session.username_email_verified or not session.website_email_verified:
            raise OnboardingValidationError(
                "Username and website emails must be verified before submit"
            )
        request = onboarding_job_from_session(
            session,
            completion_callback_url=None,
        )
        if session.submitted_job_id is not None:
            existing_job = await self.onboarding_jobs.get_job(session.submitted_job_id)
            if existing_job is not None:
                logger.info(
                    "Reusing existing onboarding job for session_id=%s job_id=%s "
                    "job_status=%s",
                    session_id,
                    existing_job.job_id,
                    existing_job.status,
                )
                return OnboardingSessionSubmission(
                    job=existing_job,
                    request=request,
                )
        job = await self.onboarding_jobs.start_job(request)
        logger.info(
            "Created onboarding job for session_id=%s job_id=%s idempotency_key=%s",
            session_id,
            job.job_id,
            job.idempotency_key,
        )
        await self.onboarding.mark_session_submitted(session_id, job_id=job.job_id)
        return OnboardingSessionSubmission(job=job, request=request)

    async def mark_session_failed(self, session_id: UUID, error: str) -> None:
        await self.onboarding.mark_session_failed(session_id, error=error)

    async def _require_session(self, session_id: UUID) -> OnboardingSessionRecord:
        session = await self.onboarding.get_session(session_id)
        if session is None:
            raise KeyError(f"Onboarding session not found: {session_id}")
        return session

    async def _validate_no_duplicate_website(
        self,
        website_url: str,
        *,
        session_id: UUID,
    ) -> None:
        domain = website_domain(website_url)
        domain_label_slug = tenant_slug(domain.split(".")[0])
        existing_tenant = await self.tenants.get_by_slug(domain_label_slug)
        if existing_tenant is not None:
            raise OnboardingValidationError(
                "A tenant with similar website or business name already exists"
            )

        existing_profile = await self.onboarding.get_business_profile_by_website_domain(
            domain,
        )
        if existing_profile is not None:
            raise OnboardingValidationError(
                "A tenant with this website already exists"
            )

        existing_session = await self.onboarding.get_active_session_by_website_domain(
            domain,
        )
        if existing_session is not None and existing_session.session_id != session_id:
            raise OnboardingValidationError(
                "An active onboarding session for this website already exists; "
                "use the resume link or contact support"
            )

    async def _send_telegram_setup_email(
        self,
        session: OnboardingSessionRecord,
        setup_url: str,
    ) -> None:
        if not self.settings.onboarding_review_email:
            logger.info(
                "Skipping SaaS-admin Telegram setup email because no onboarding review "
                "email is configured session_id=%s",
                session.session_id,
            )
            return

        business_name = (
            session.business_profile.business_name
            if session.business_profile
            else "New customer-service tenant"
        )
        await self.email_sender.send_email(
            to=[self.settings.onboarding_review_email],
            subject=f"Telegram setup needed for {business_name}",
            text=telegram_setup_email_text(session, setup_url),
        )


def validate_website_verification_fields(
    website_url: str,
    website_verification_email: str,
    *,
    require_email_domain_match: bool = True,
) -> None:
    parsed = urlparse(website_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OnboardingValidationError("website_url must be a valid HTTP/HTTPS URL")

    email_domain = domain_without_www(website_verification_email.split("@")[-1])
    website_domain = domain_without_www(parsed.hostname or "")
    if not email_domain or not website_domain:
        raise OnboardingValidationError(
            "website verification email and website domain are required"
        )

    if require_email_domain_match and not domains_match(email_domain, website_domain):
        raise OnboardingValidationError(
            "website verification email domain must belong to the website domain"
        )


def website_domain(website_url: str) -> str:
    parsed = urlparse(website_url)
    return domain_without_www(parsed.hostname or "")


def domains_match(email_domain: str, website_domain: str) -> bool:
    return (
        email_domain == website_domain
        or email_domain.endswith(f".{website_domain}")
        or website_domain.endswith(f".{email_domain}")
    )


def domain_without_www(value: str) -> str:
    return value.strip().lower().removeprefix("www.")


def telegram_setup_url(base_url: str, session_id: UUID, token: str) -> str:
    return (
        f"{base_url.rstrip('/')}/telegram-setup"
        f"?session_id={session_id}&token={token}"
    )


def username_email_verification_url(base_url: str, session_id: UUID, token: str) -> str:
    return (
        f"{base_url.rstrip('/')}/verify-username-email"
        f"?session_id={session_id}&token={token}"
    )


def website_email_verification_url(base_url: str, session_id: UUID, token: str) -> str:
    return (
        f"{base_url.rstrip('/')}/verify-website-email"
        f"?session_id={session_id}&token={token}"
    )


def onboarding_resume_url(base_url: str, session_id: UUID) -> str:
    return f"{base_url.rstrip('/')}?session_id={session_id}"


def telegram_setup_email_text(
    session: OnboardingSessionRecord,
    setup_url: str,
) -> str:
    business_name = (
        session.business_profile.business_name
        if session.business_profile
        else "New customer-service tenant"
    )
    lines = [
        "A customer-service tenant has reached the Telegram setup step.",
        "",
        f"Business: {business_name}",
        "",
        f"Setup link: {setup_url}",
        "",
        "Open the setup link to review the onboarding details and enter the tenant "
        "Telegram bot credentials.",
    ]
    return "\n".join(lines)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def email_verification_rejection_reason(
    diagnostic: OnboardingEmailVerificationDiagnostic,
) -> str:
    if not diagnostic.session_exists:
        return "session_missing"
    if diagnostic.admin_email_verified:
        return "already_verified"
    if not diagnostic.has_token_hash:
        return "token_missing"
    if diagnostic.token_used:
        return "token_already_used"
    if diagnostic.token_expired:
        return "token_expired"
    if not diagnostic.token_matches:
        return "token_hash_mismatch"
    return "token_state_valid_but_consume_failed"


def onboarding_job_from_session(
    session: OnboardingSessionRecord,
    *,
    completion_callback_url: str | None,
) -> OnboardingJobCreate:
    missing = []
    if not session.business_profile:
        missing.append("business_profile")
    if not session.business_summary:
        missing.append("business_summary")
    if not session.telegram:
        missing.append("telegram")
    if missing:
        raise OnboardingValidationError(
            "Onboarding session is incomplete: " + ", ".join(missing)
        )

    business_slug = tenant_slug(session.business_profile.business_name)
    return OnboardingJobCreate(
        idempotency_key=f"onboarding-session-{session.session_id}",
        callback_url=completion_callback_url,
        selected_plan=DEFAULT_TENANT_PLAN,
        admin=session.admin,
        business_profile=session.business_profile,
        business_summary=session.business_summary,
        contact_info=[
            point for point in session.contact_info if point.kind != "website"
        ],
        telegram=session.telegram,
        provider_projects=session.provider_projects.model_copy(
            update={
                "llm_project_name": (
                    session.provider_projects.llm_project_name
                    or f"customer-service-{business_slug}"
                ),
                "langsmith_project": (
                    session.provider_projects.langsmith_project
                    or f"customer-service-{business_slug}"
                ),
            }
        ),
        knowledge_sources=session.knowledge_sources,
    )
