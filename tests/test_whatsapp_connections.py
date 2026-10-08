import asyncio
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
from pydantic import SecretStr

from app.adapters.credential_store import CredentialNotFound
from app.adapters.meta_signup import VerifiedWhatsAppAsset
from app.whatsapp_connections import PostgresWhatsAppConnections
from app.whatsapp_signup import WhatsAppSignupError, WhatsAppSignupService


@pytest.mark.parametrize("legacy_verified", [False, True])
async def test_email_verification_and_browser_authorization_survive_startup(legacy_verified):
    from app.adapters.postgres import PostgresOnboardingRepository, schema

    url = os.getenv("AGENT_WHATSAPP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set AGENT_WHATSAPP_TEST_DATABASE_URL to a disposable pgvector database")
    namespace = "verification_restart_" + uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    await admin.execute(f'CREATE SCHEMA "{namespace}"')
    pool = await asyncpg.create_pool(url, server_settings={"search_path": f"{namespace},public"})
    try:
        await pool.execute(schema(64))
        repo = PostgresOnboardingRepository(SimpleNamespace(pool=pool))
        session = await pool.fetchval(
            "INSERT INTO onboarding_sessions(admin_email,username_email_verification_token_hash, "
            "username_email_verification_expires_at) VALUES('test@example.com','test-hash', "
            "now()+interval '10 minutes') RETURNING session_id"
        )
        assert await repo.consume_username_email_verification_token(
            session, token_hash="test-hash", max_attempts=5
        )
        connections = PostgresWhatsAppConnections(pool)
        await connections.initialize()
        signup = WhatsAppSignupService(connections, Credentials(), Meta())
        browser = await signup.authorize_verified_browser(session)
        await pool.execute(
            "UPDATE onboarding_sessions SET admin_email_verified=$2 WHERE session_id=$1",
            session,
            legacy_verified,
        )
        # A pending replacement code must not inherit a retired verified flag either.
        pending = await pool.fetchval(
            "INSERT INTO onboarding_sessions(admin_email,admin_email_verified, "
            "username_email_verification_token_hash,username_email_verification_expires_at) "
            "VALUES('pending@example.com',true,'pending-hash',now()+interval '10 minutes') "
            "RETURNING session_id"
        )
        # End all application connections, then run actual database initialization
        # in fresh interpreters, as successive application process restarts would.
        await pool.close()
        for _ in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import asyncio, os; from app.adapters.postgres import PostgresDatabase\n"
                "async def start():\n"
                " db = PostgresDatabase(os.environ['AGENT_WHATSAPP_TEST_DATABASE_URL'], "
                "connect_kwargs={'server_settings': {'search_path': "
                "os.environ['VERIFICATION_TEST_SCHEMA'] + ',public'}})\n"
                " await db.initialize()\n"
                " await db.close()\n"
                "asyncio.run(start())\n",
                env={**os.environ, "VERIFICATION_TEST_SCHEMA": namespace},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                raise
            assert process.returncode == 0, stderr.decode()
        pool = await asyncpg.create_pool(
            url, server_settings={"search_path": f"{namespace},public"}
        )
        signup = WhatsAppSignupService(PostgresWhatsAppConnections(pool), Credentials(), Meta())
        assert (
            await pool.fetchval(
                "SELECT username_email_verified FROM onboarding_sessions WHERE session_id=$1",
                session,
            )
            is True
        )
        await signup.require_browser(session, browser)
        assert (
            await pool.fetchval(
                "SELECT username_email_verified FROM onboarding_sessions WHERE session_id=$1",
                pending,
            )
            is False
        )
        assert (
            await pool.fetchval(
                "SELECT username_email_verification_token_hash FROM onboarding_sessions "
                "WHERE session_id=$1",
                pending,
            )
            == "pending-hash"
        )
        with pytest.raises(WhatsAppSignupError):
            await signup.require_browser(session, "wrong-browser")
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{namespace}" CASCADE')
        await admin.close()


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
        await pool.execute(
            "CREATE TABLE conversations(id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
            "tenant_id text,channel text,external_chat_id text,external_user_id text, "
            "UNIQUE(tenant_id,channel,external_chat_id)); "
            "CREATE TABLE messages(id uuid PRIMARY KEY DEFAULT "
            "gen_random_uuid(),tenant_id text,conversation_id uuid,event_id text, "
            "sender_type text,body text,created_at timestamptz,UNIQUE(tenant_id,event_id))"
        )
        await repo.initialize()
        await repo.initialize()
        yield repo
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_browser_recovery_limits_binding_and_replay(connections, monkeypatch):
    import re
    from unittest.mock import AsyncMock

    import httpx
    from fastapi import FastAPI

    from app.api import browser_recovery

    session_id = uuid4()
    await connections.pool.execute(
        "INSERT INTO onboarding_sessions(session_id) VALUES($1)", session_id
    )
    session = SimpleNamespace(
        username_email_verified=True,
        admin=SimpleNamespace(username_email="owner@example.com", name="Owner"),
    )
    email = SimpleNamespace(send_email=AsyncMock())
    app = FastAPI()
    app.include_router(browser_recovery.router)
    signup = WhatsAppSignupService(connections, Credentials(), Meta())
    app.state.container = SimpleNamespace(
        whatsapp_signup=signup,
        onboarding_sessions=SimpleNamespace(
            get_session=AsyncMock(return_value=session), email_sender=email
        ),
    )
    monkeypatch.setattr(
        browser_recovery,
        "get_settings",
        lambda: SimpleNamespace(
            web_public_base_url="https://test.example",
            onboarding_verification_code_secret="test-secret",
        ),
    )
    base = f"/onboarding/sessions/{session_id}/browser"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test.example",
        headers={"Origin": "https://test.example"},
    ) as client:
        assert (
            await client.post(base + "/send-code", headers={"Origin": "https://evil.example"})
        ).status_code == 403
        assert (await client.post(base + "/send-code")).status_code == 200
        code = re.search(r"Enter (\d{6})", email.send_email.call_args.kwargs["text"])[1]
        assert (await client.post(base + "/send-code")).status_code == 429
        cookies = httpx.Cookies(client.cookies)
        client.cookies.clear()
        assert (await client.post(base + "/verify-code", json={"code": code})).status_code == 422
        client.cookies = cookies
        wrong = "000000" if code != "000000" else "111111"
        for expected in [422, 422, 422, 422, 429]:
            assert (
                await client.post(base + "/verify-code", json={"code": wrong})
            ).status_code == expected
        assert (await client.post(base + "/verify-code", json={"code": code})).status_code == 429
        await connections.pool.execute(
            "UPDATE onboarding_browser_challenges SET resend_at=now()-interval '1 second'"
        )
        assert (await client.post(base + "/send-code")).status_code == 200
        code = re.search(r"Enter (\d{6})", email.send_email.call_args.kwargs["text"])[1]
        recovery_cookie = httpx.Cookies(client.cookies)
        assert (await client.post(base + "/verify-code", json={"code": code})).status_code == 200
        await signup.require_browser(session_id, client.cookies[f"onboarding-{session_id}"])
        client.cookies = recovery_cookie
        assert (await client.post(base + "/verify-code", json={"code": code})).status_code == 422
        await connections.pool.execute(
            "UPDATE onboarding_browser_challenges SET resend_at=now()-interval '1 second'"
        )
        assert (await client.post(base + "/send-code")).status_code == 200
        code = re.search(r"Enter (\d{6})", email.send_email.call_args.kwargs["text"])[1]
        await connections.pool.execute(
            "UPDATE onboarding_browser_challenges SET expires_at=now()-interval '1 second'"
        )
        assert (await client.post(base + "/verify-code", json={"code": code})).status_code == 422
        assert await connections.pool.fetchval(
            "SELECT username_email_verified FROM onboarding_sessions WHERE session_id=$1",
            session_id,
        )


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
        return VerifiedWhatsAppAsset(waba_id, phone_number_id, "+254700000001", True)

    async def subscribe(self, token, waba_id):
        self.subscriptions += 1

    async def register(self, token, phone_number_id, pin):
        raise AssertionError("Coexistence must not re-register a number")

    async def request_history(self, token, phone_number_id):
        if self.fail:
            raise RuntimeError("secret-provider-payload")
        self.registrations += 1


async def setup(repo, *, import_history=True):
    session = uuid4()
    await repo.pool.execute("INSERT INTO onboarding_sessions(session_id) VALUES($1)", session)
    if import_history:
        drive_id = uuid4()
        await repo.pool.execute(
            "INSERT INTO drive_connections(id,session_id,credential_reference,root_name,status) "
            "VALUES($1,$2,$3,'Test archive','ready')",
            drive_id,
            session,
            f"google-drive/{drive_id}",
        )
        await repo.pool.execute(
            "INSERT INTO whatsapp_archive_preferences(session_id,import_enabled,drive_id) "
            "VALUES($1,true,$2)",
            session,
            drive_id,
        )
    meta = Meta()
    service = WhatsAppSignupService(repo, Credentials(), meta)
    browser = await service.authorize_verified_browser(session)
    return service, session, browser, meta


async def test_cancel_unlocks_only_latest_unconnected_attempt(connections):
    service, session, browser, meta = await setup(connections)
    first = await service.start(session, browser)
    args = {key: first[key] for key in ("attempt_id", "state")}
    with pytest.raises(WhatsAppSignupError):
        await service.cancel(session, "wrong-browser", **args)
    assert await service.cancel(session, browser, **args) == {"locked": False}
    assert not await connections.pool.fetchval(
        "SELECT locked FROM whatsapp_archive_preferences WHERE session_id=$1", session
    )
    with pytest.raises(WhatsAppSignupError):
        await service.complete(
            session, browser, **args, code=SecretStr("code"), waba_id="456", phone_number_id="789"
        )
    second = await service.start(session, browser)
    assert await service.cancel(session, browser, **args) == {"locked": True}
    args = {key: second[key] for key in ("attempt_id", "state")}
    await service.complete(
        session, browser, **args, code=SecretStr("code"), waba_id="456", phone_number_id="789"
    )
    assert await service.cancel(session, browser, **args) == {"locked": True}
    assert await connections.pool.fetchval(
        "SELECT locked FROM whatsapp_archive_preferences WHERE session_id=$1", session
    )


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


async def test_partial_retry_preserves_progress_without_registration_pin(connections):
    service, session, browser, meta = await setup(connections)
    meta.fail = True
    with pytest.raises(WhatsAppSignupError, match="^connection_setup_failed$"):
        await complete(service, session, browser)
    before = await connections.for_session(session)
    assert (await service.credentials.read(before.connection_id)).registration_pin is None
    assert before.status == "failed"
    meta.fail = False
    after = await complete(service, session, browser)
    assert after.connection_id == before.connection_id
    assert (await service.credentials.read(after.connection_id)).registration_pin is None
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


async def test_history_before_activation_and_echo_pause_are_idempotent(connections):
    from app.whatsapp_coexistence import handle_coexistence

    service, session, browser, _ = await setup(connections)
    result = await complete(service, session, browser)
    history = {
        "history": [
            {
                "threads": [
                    {
                        "id": "254700000001",
                        "messages": [
                            {
                                "id": "old-message",
                                "from": "254700000001",
                                "timestamp": "1700000000",
                                "type": "text",
                                "text": {"body": "Previous order"},
                            },
                        ],
                    }
                ]
            }
        ]
    }
    await handle_coexistence(connections, "wrong", "789", "history", history)
    assert await connections.pool.fetchval("SELECT count(*) FROM whatsapp_imported_messages") == 0
    await handle_coexistence(connections, "456", "789", "history", history)
    await handle_coexistence(connections, "456", "789", "history", history)
    assert await connections.pool.fetchval("SELECT count(*) FROM whatsapp_imported_messages") == 1
    assert await connections.pool.fetchval("SELECT count(*) FROM messages") == 0
    assert not await connections.is_paused("456", "789", "254700000001")
    job = uuid4()
    await connections.pool.execute("INSERT INTO tenants VALUES('tenant')")
    await connections.pool.execute(
        "UPDATE onboarding_sessions SET submitted_job_id=$2 WHERE session_id=$1", session, job
    )
    await connections.pool.execute("INSERT INTO onboarding_jobs(job_id) VALUES($1)", job)
    await connections.activate(result.connection_id, job, "tenant", "bakery")
    assert await connections.pool.fetchval("SELECT count(*) FROM messages") == 1
    assert not await connections.claim_message("456", "789", "whatsapp:old-message", active=True)
    echo = {
        "message_echoes": [
            {
                "id": "human",
                "from": "254700000002",
                "to": "254700000001",
                "timestamp": "1700000001",
                "type": "text",
                "text": {"body": "I can help"},
            }
        ]
    }
    await handle_coexistence(connections, "456", "789", "smb_message_echoes", echo)
    await handle_coexistence(connections, "456", "789", "smb_message_echoes", echo)
    assert await connections.is_paused("456", "789", "254700000001")
    assert not await connections.is_paused("456", "789", "254700000003")
    assert await connections.pool.fetchval("SELECT count(*) FROM messages") == 2
    await connections.pool.execute(
        "UPDATE whatsapp_connections SET pause_on_human_reply=false WHERE connection_id=$1",
        result.connection_id,
    )
    echo["message_echoes"][0].update(id="human-2", to="254700000003")
    await handle_coexistence(connections, "456", "789", "smb_message_echoes", echo)
    assert not await connections.is_paused("456", "789", "254700000003")
    assert await connections.is_paused("456", "789", "254700000001")


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
