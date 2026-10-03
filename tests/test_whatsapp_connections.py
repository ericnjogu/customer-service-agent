import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from pydantic import SecretStr

from app.adapters.credential_store import CredentialNotFound
from app.adapters.meta_signup import VerifiedWhatsAppAsset
from app.whatsapp_connections import PostgresWhatsAppConnections
from app.whatsapp_signup import WhatsAppSignupError, WhatsAppSignupService


@pytest.fixture
async def connections():
    url = os.getenv("AGENT_WHATSAPP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set AGENT_WHATSAPP_TEST_DATABASE_URL to a disposable PostgreSQL database")
    schema = "whatsapp_test_" + uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(url, server_settings={"search_path": schema}, min_size=1)
    try:
        await pool.execute(
            "CREATE TABLE tenants(tenant_id text PRIMARY KEY); "
            "CREATE TABLE onboarding_sessions(session_id uuid PRIMARY KEY, "
            "submitted_job_id uuid, username_email_verified boolean NOT NULL DEFAULT true); "
            "CREATE TABLE onboarding_jobs(job_id uuid PRIMARY KEY,status text,tenant_id text, "
            "tenant_slug text,error text,updated_at timestamptz)"
        )
        repo = PostgresWhatsAppConnections(pool)
        await repo.initialize()
        await repo.initialize()
        yield repo
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


class Credentials:
    def __init__(self):
        self.values = {}

    async def write(self, key, value):
        self.values[key] = value

    async def read(self, key):
        if key not in self.values:
            raise CredentialNotFound("missing")
        return self.values[key]


class Meta:
    def __init__(self):
        self.subscriptions = 0
        self.registrations = 0
        self.fail = False

    async def exchange(self, code):
        return SecretStr("customer-token")

    async def verify_asset(self, token, *, waba_id, phone_number_id):
        return VerifiedWhatsAppAsset(waba_id, phone_number_id, "+254700000001")

    async def subscribe(self, token, waba_id):
        self.subscriptions += 1

    async def register(self, token, phone_number_id, pin):
        if self.fail:
            raise RuntimeError("secret-provider-payload")
        self.registrations += 1


async def setup(repo):
    session = uuid4()
    await repo.pool.execute("INSERT INTO onboarding_sessions(session_id) VALUES($1)", session)
    meta = Meta()
    service = WhatsAppSignupService(repo, Credentials(), meta)
    browser = await service.authorize_verified_browser(session)
    return service, session, browser, meta


async def complete(service, session, browser, phone="789"):
    attempt = await service.start(session, browser)
    return await service.complete(
        session,
        browser,
        attempt_id=attempt["attempt_id"],
        state=attempt["state"],
        code=SecretStr("code"),
        waba_id="456",
        phone_number_id=phone,
    )


async def test_connection_does_not_activate_before_provisioning(connections):
    service, session, browser, meta = await setup(connections)
    result = await complete(service, session, browser)
    assert result.status == "connected"
    assert result.tenant_id is None
    assert await connections.resolve_active("456", "789") is None
    assert result.credential_reference == f"whatsapp/{result.connection_id}"
    assert "customer-token" not in result.model_dump_json()
    assert meta.subscriptions == meta.registrations == 1


async def test_activation_and_job_success_are_atomic(connections):
    service, session, browser, _ = await setup(connections)
    result = await complete(service, session, browser)
    job = uuid4()
    await connections.pool.execute("INSERT INTO tenants VALUES('tenant')")
    await connections.pool.execute(
        "UPDATE onboarding_sessions SET submitted_job_id=$2 WHERE session_id=$1", session, job
    )
    with pytest.raises(ValueError, match="job no longer exists"):
        await connections.activate(result.connection_id, job, "tenant", "bakery")
    assert (await connections.for_session(session)).status == "connected"
    await connections.pool.execute(
        "INSERT INTO onboarding_jobs(job_id,status) VALUES($1,'running')", job
    )
    await connections.activate(result.connection_id, job, "tenant", "bakery")
    assert await connections.resolve_active("456", "789") == "tenant"
    assert (
        await connections.pool.fetchval("SELECT status FROM onboarding_jobs WHERE job_id=$1", job)
        == "succeeded"
    )
    await connections.activate(result.connection_id, job, "tenant", "bakery")
    with pytest.raises(ValueError, match="does not belong"):
        await connections.activate(result.connection_id, uuid4(), "tenant", "bakery")


async def test_provisioning_lock_excludes_concurrent_replica_and_releases(connections):
    job = uuid4()
    async with connections.provisioning_lock(job) as first:
        assert first
        async with connections.provisioning_lock(job) as second:
            assert not second
        async with connections.provisioning_lock(uuid4()) as other:
            assert other
    async with connections.provisioning_lock(job) as retry:
        assert retry


async def test_replay_and_wrong_browser_are_rejected(connections):
    service, session, browser, _ = await setup(connections)
    attempt = await service.start(session, browser)
    args = dict(
        attempt_id=attempt["attempt_id"],
        state=attempt["state"],
        code=SecretStr("code"),
        waba_id="456",
        phone_number_id="789",
    )
    with pytest.raises(WhatsAppSignupError, match="verified_browser_required"):
        await service.complete(session, "wrong-browser", **args)
    await service.complete(session, browser, **args)
    with pytest.raises(WhatsAppSignupError, match="signup_attempt_invalid_or_used"):
        await service.complete(session, browser, **args)


async def test_partial_retry_preserves_progress_and_pin(connections):
    service, session, browser, meta = await setup(connections)
    meta.fail = True
    with pytest.raises(WhatsAppSignupError, match="^connection_setup_failed$"):
        await complete(service, session, browser)
    before = await connections.for_session(session)
    pin = (await service.credentials.read(before.connection_id)).registration_pin
    assert before.status == "failed"
    meta.fail = False
    after = await complete(service, session, browser)
    assert after.connection_id == before.connection_id
    assert (await service.credentials.read(after.connection_id)).registration_pin == pin
    assert meta.subscriptions == meta.registrations == 1


async def test_same_waba_multiple_numbers_but_unique_phone(connections):
    first, s1, b1, _ = await setup(connections)
    second, s2, b2, _ = await setup(connections)
    await complete(first, s1, b1)
    with pytest.raises(WhatsAppSignupError, match="phone_already_assigned"):
        await complete(second, s2, b2)
    result = await complete(second, s2, b2, phone="999")
    assert result.waba_id == "456"


async def test_duplicate_webhook_claim_is_atomic_and_inactive_is_not_replayed(connections):
    outcomes = await asyncio.gather(
        *[connections.claim_message("456", "789", "message", active=True) for _ in range(10)]
    )
    assert sum(outcomes) == 1
    assert await connections.claim_message("456", "789", "inactive", active=False)
    assert not await connections.claim_message("456", "789", "inactive", active=True)


async def test_new_attempt_invalidates_old_and_expired_attempts_fail(connections):
    service, session, browser, _ = await setup(connections)
    old = await service.start(session, browser)
    new = await service.start(session, browser)
    await connections.pool.execute("UPDATE whatsapp_signup_attempts SET expires_at=now()")
    for attempt in (old, new):
        with pytest.raises(WhatsAppSignupError, match="signup_attempt_invalid_or_used"):
            await service.complete(
                session,
                browser,
                attempt_id=attempt["attempt_id"],
                state=attempt["state"],
                code=SecretStr("code"),
                waba_id="456",
                phone_number_id="789",
            )
