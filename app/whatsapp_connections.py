"""Persisted signup state. No Meta credentials belong in this module."""

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class WhatsAppConnection(BaseModel):
    connection_id: UUID
    session_id: UUID
    tenant_id: str | None = None
    waba_id: str | None = None
    phone_number_id: str | None = None
    display_number: str | None = None
    credential_reference: str
    status: Literal["connecting", "connected", "active", "failed"]
    error_code: str | None = None
    subscribed_at: datetime | None = None
    registered_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


WHATSAPP_SCHEMA = """
CREATE TABLE IF NOT EXISTS whatsapp_connections (
    connection_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL UNIQUE REFERENCES onboarding_sessions(session_id),
    tenant_id text UNIQUE REFERENCES tenants(tenant_id),
    waba_id text,
    phone_number_id text UNIQUE,
    display_number text,
    credential_reference text NOT NULL UNIQUE,
    status text NOT NULL DEFAULT 'connecting'
        CHECK (status IN ('connecting', 'connected', 'active', 'failed')),
    error_code text,
    subscribed_at timestamptz,
    registered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status NOT IN ('connected', 'active') OR
        (waba_id IS NOT NULL AND phone_number_id IS NOT NULL AND display_number IS NOT NULL
         AND subscribed_at IS NOT NULL AND registered_at IS NOT NULL)),
    CHECK (status <> 'active' OR tenant_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS whatsapp_signup_attempts (
    attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES onboarding_sessions(session_id),
    browser_hash text NOT NULL,
    state_hash text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS whatsapp_signup_attempts_session
    ON whatsapp_signup_attempts(session_id);
CREATE TABLE IF NOT EXISTS onboarding_browser_sessions (
    session_id uuid PRIMARY KEY REFERENCES onboarding_sessions(session_id),
    browser_hash text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS whatsapp_webhook_receipts (
    waba_id text NOT NULL,
    phone_number_id text NOT NULL,
    message_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('processing', 'sent', 'failed', 'ignored')),
    received_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (waba_id, phone_number_id, message_id)
);
"""


class PostgresWhatsAppConnections:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self) -> None:
        await self.pool.execute(WHATSAPP_SCHEMA)

    @asynccontextmanager
    async def provisioning_lock(self, job_id: UUID):
        # Session-level lock is automatically released if the process/connection dies.
        # Never hold a SQL transaction open across provider network requests.
        key = f"whatsapp-provisioning:{job_id}"
        async with self.pool.acquire() as sql:
            acquired = await sql.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1,0))", key
            )
            try:
                yield acquired
            finally:
                if acquired:
                    await sql.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)

    async def for_session(self, session_id: UUID) -> WhatsAppConnection | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM whatsapp_connections WHERE session_id=$1", session_id
        )
        return WhatsAppConnection.model_validate(dict(row)) if row else None

    async def activate(self, connection_id: UUID, job_id: UUID, tenant_id: str, slug: str) -> None:
        async with self.pool.acquire() as sql, sql.transaction():
            changed = await sql.fetchval(
                "UPDATE whatsapp_connections c SET tenant_id=$3,status='active',updated_at=now() "
                "FROM onboarding_sessions s WHERE c.connection_id=$1 AND c.session_id=s.session_id "
                "AND s.submitted_job_id=$2 AND c.status IN ('connected','active') "
                "AND (c.tenant_id IS NULL OR c.tenant_id=$3) RETURNING c.connection_id",
                connection_id,
                job_id,
                tenant_id,
            )
            if not changed:
                raise ValueError("Verified connection does not belong to this provisioning job")
            completed = await sql.fetchval(
                "UPDATE onboarding_jobs SET status='succeeded',tenant_id=$2,tenant_slug=$3, "
                "error=NULL,updated_at=now() WHERE job_id=$1 RETURNING job_id",
                job_id,
                tenant_id,
                slug,
            )
            if not completed:
                raise ValueError("Provisioning job no longer exists")

    async def for_tenant(self, tenant_id: str) -> WhatsAppConnection | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM whatsapp_connections WHERE tenant_id=$1 AND status='active'", tenant_id
        )
        return WhatsAppConnection.model_validate(dict(row)) if row else None

    async def resolve_active(self, waba_id: str, phone_number_id: str) -> str | None:
        return await self.pool.fetchval(
            "SELECT tenant_id FROM whatsapp_connections "
            "WHERE waba_id=$1 AND phone_number_id=$2 AND status='active'",
            waba_id,
            phone_number_id,
        )

    async def consume_attempt(
        self, attempt_id: UUID, session_id: UUID, browser_hash: str, state_hash: str
    ) -> bool:
        return bool(
            await self.pool.fetchval(
                "UPDATE whatsapp_signup_attempts SET consumed_at=now() "
                "WHERE attempt_id=$1 AND session_id=$2 AND browser_hash=$3 AND state_hash=$4 "
                "AND consumed_at IS NULL AND expires_at>now() RETURNING attempt_id",
                attempt_id,
                session_id,
                browser_hash,
                state_hash,
            )
        )

    async def claim_message(
        self, waba_id: str, phone_number_id: str, message_id: str, *, active: bool
    ) -> bool:
        # Never retry unknown outbound delivery: Meta sends lack an idempotency key.
        # Retain processing/failed receipts for diagnosis instead of duplicate replies.
        return bool(
            await self.pool.fetchval(
                "INSERT INTO whatsapp_webhook_receipts "
                "(waba_id,phone_number_id,message_id,status) VALUES($1,$2,$3,$4) "
                "ON CONFLICT DO NOTHING RETURNING message_id",
                waba_id,
                phone_number_id,
                message_id,
                "processing" if active else "ignored",
            )
        )
