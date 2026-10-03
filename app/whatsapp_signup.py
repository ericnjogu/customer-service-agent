"""Browser-bound, single-use signup orchestration; activation is a separate step."""

import hashlib
import secrets
from uuid import UUID, uuid4

import asyncpg
from pydantic import SecretStr

from app.adapters.credential_store import (
    ConnectionCredentials,
    CredentialNotFound,
    CredentialStore,
)
from app.adapters.meta_signup import MetaSignupClient
from app.whatsapp_connections import PostgresWhatsAppConnections, WhatsAppConnection


class WhatsAppSignupError(ValueError):
    pass


def capability_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class WhatsAppSignupService:
    def __init__(
        self,
        repository: PostgresWhatsAppConnections,
        credentials: CredentialStore,
        meta: MetaSignupClient,
    ):
        self.repository = repository
        self.credentials = credentials
        self.meta = meta

    async def authorize_verified_browser(self, session_id: UUID) -> str:
        """Call only immediately after successful email code consumption."""
        token = secrets.token_urlsafe(32)
        result = await self.repository.pool.fetchval(
            "INSERT INTO onboarding_browser_sessions(session_id,browser_hash,expires_at) "
            "SELECT session_id,$2,now()+interval '24 hours' FROM onboarding_sessions "
            "WHERE session_id=$1 AND username_email_verified "
            "ON CONFLICT(session_id) DO UPDATE SET browser_hash=EXCLUDED.browser_hash, "
            "expires_at=EXCLUDED.expires_at RETURNING session_id",
            session_id,
            capability_hash(token),
        )
        if result is None:
            raise WhatsAppSignupError("account_verification_required")
        return token

    async def require_browser(self, session_id: UUID, browser: str) -> None:
        valid = await self.repository.pool.fetchval(
            "SELECT 1 FROM onboarding_browser_sessions b JOIN onboarding_sessions s "
            "USING(session_id) WHERE b.session_id=$1 AND b.browser_hash=$2 "
            "AND b.expires_at>now() AND s.username_email_verified",
            session_id,
            capability_hash(browser),
        )
        if not valid:
            raise WhatsAppSignupError("verified_browser_required")

    async def start(self, session_id: UUID, browser: str) -> dict:
        await self.require_browser(session_id, browser)
        state = secrets.token_urlsafe(32)
        async with self.repository.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(session_id)
            )
            await connection.execute(
                "UPDATE whatsapp_signup_attempts SET consumed_at=now() "
                "WHERE session_id=$1 AND consumed_at IS NULL",
                session_id,
            )
            row = await connection.fetchrow(
                "INSERT INTO whatsapp_signup_attempts "
                "(session_id,browser_hash,state_hash,expires_at) "
                "VALUES($1,$2,$3,now()+interval '10 minutes') RETURNING attempt_id,expires_at",
                session_id,
                capability_hash(browser),
                capability_hash(state),
            )
        return {"attempt_id": row["attempt_id"], "state": state, "expires_at": row["expires_at"]}

    async def complete(
        self,
        session_id: UUID,
        browser: str,
        *,
        attempt_id: UUID,
        state: str,
        code: SecretStr,
        waba_id: str,
        phone_number_id: str,
    ):
        await self.require_browser(session_id, browser)
        if not await self.repository.consume_attempt(
            attempt_id, session_id, capability_hash(browser), capability_hash(state)
        ):
            raise WhatsAppSignupError("signup_attempt_invalid_or_used")
        # Validate BEFORE assigning the globally unique phone number. A malicious
        # browser must not reserve arbitrary numbers belonging to another client.
        token = await self.meta.exchange(code)
        asset = await self.meta.verify_asset(
            token, waba_id=waba_id, phone_number_id=phone_number_id
        )
        try:
            async with self.repository.pool.acquire() as connection:
                # Session-level lock spans independent durable progress commits.
                # Crash/retry retains successful Meta steps without holding an SQL transaction.
                await connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended($1,0))", str(session_id)
                )
                try:
                    return await self._connect(connection, session_id, token, asset)
                finally:
                    await connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended($1,0))", str(session_id)
                    )
        except asyncpg.UniqueViolationError:
            raise WhatsAppSignupError("phone_already_assigned") from None

    async def _connect(self, sql, session_id, token, asset):
        row = await sql.fetchrow(
            "SELECT * FROM whatsapp_connections WHERE session_id=$1", session_id
        )
        existing = WhatsAppConnection.model_validate(dict(row)) if row else None
        if (
            existing
            and existing.phone_number_id
            and (
                existing.phone_number_id != asset.phone_number_id
                or existing.waba_id != asset.waba_id
            )
        ):
            raise WhatsAppSignupError("connection_asset_change_not_supported")
        if existing and existing.status == "active":
            raise WhatsAppSignupError("connection_already_active")
        connection_id = existing.connection_id if existing else uuid4()
        reference = f"whatsapp/{connection_id}"
        await sql.execute(
            "INSERT INTO whatsapp_connections(connection_id,session_id,waba_id,phone_number_id, "
            "display_number,credential_reference) VALUES($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT(session_id) DO UPDATE SET waba_id=EXCLUDED.waba_id, "
            "phone_number_id=EXCLUDED.phone_number_id,display_number=EXCLUDED.display_number, "
            "status='connecting',error_code=NULL,updated_at=now()",
            connection_id,
            session_id,
            asset.waba_id,
            asset.phone_number_id,
            asset.display_number,
            reference,
        )
        try:
            # Preserve the PIN on retries after registration; do not reset it.
            try:
                previous = await self.credentials.read(connection_id) if existing else None
            except CredentialNotFound:
                if existing and existing.registered_at:
                    raise
                previous = None
            pin = (
                previous.registration_pin
                if previous
                else SecretStr(f"{secrets.randbelow(10**6):06d}")
            )
            await self.credentials.write(
                connection_id,
                ConnectionCredentials(
                    access_token=token,
                    registration_pin=pin,
                ),
            )
            if not existing or not existing.subscribed_at:
                await self.meta.subscribe(token, asset.waba_id)
                await sql.execute(
                    "UPDATE whatsapp_connections SET subscribed_at=now() WHERE connection_id=$1",
                    connection_id,
                )
            if not existing or not existing.registered_at:
                await self.meta.register(token, asset.phone_number_id, pin)
                await sql.execute(
                    "UPDATE whatsapp_connections SET registered_at=now() WHERE connection_id=$1",
                    connection_id,
                )
            await sql.execute(
                "UPDATE whatsapp_connections SET status='connected',updated_at=now() "
                "WHERE connection_id=$1",
                connection_id,
            )
        except Exception:
            await sql.execute(
                "UPDATE whatsapp_connections SET status='failed', "
                "error_code='connection_setup_failed',updated_at=now() "
                "WHERE connection_id=$1",
                connection_id,
            )
            raise WhatsAppSignupError("connection_setup_failed") from None
        row = await sql.fetchrow(
            "SELECT * FROM whatsapp_connections WHERE session_id=$1", session_id
        )
        return WhatsAppConnection.model_validate(dict(row))
