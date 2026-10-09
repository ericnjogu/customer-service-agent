"""Browser-bound, single-use signup orchestration; activation is a separate step."""

import hashlib
import secrets
from uuid import UUID, uuid4

import asyncpg
from pydantic import SecretStr

from app.adapters.credential_store import (
    ConnectionCredentials,
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
        row = await self.repository.pool.fetchrow(
            "SELECT s.username_email_verified, b.session_id IS NOT NULL AS browser_exists, "
            "b.browser_hash=$2 AS browser_matches, b.expires_at>now() AS browser_unexpired, "
            "b.expires_at FROM onboarding_sessions s LEFT JOIN onboarding_browser_sessions b "
            "USING(session_id) WHERE s.session_id=$1",
            session_id,
            capability_hash(browser),
        )
        valid = bool(
            browser
            and row
            and row["username_email_verified"]
            and row["browser_exists"]
            and row["browser_matches"]
            and row["browser_unexpired"]
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
                "INSERT INTO "
                "whatsapp_archive_preferences(session_id) VALUES($1) ON CONFLICT DO NOTHING",
                session_id,
            )
            preference = await connection.fetchrow(
                "SELECT * FROM whatsapp_archive_preferences WHERE session_id=$1 FOR UPDATE",
                session_id,
            )
            if preference["import_enabled"] and not await connection.fetchval(
                "SELECT 1 FROM drive_connections WHERE id=$1 AND session_id=$2 AND status='ready'",
                preference["drive_id"],
                session_id,
            ):
                raise WhatsAppSignupError("drive_connection_required")
            await connection.execute(
                "UPDATE whatsapp_archive_preferences SET locked=true WHERE session_id=$1",
                session_id,
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
        try:
            async with self.repository.pool.acquire() as connection:
                # Session-level lock spans independent durable progress commits.
                # Crash/retry retains successful Meta steps without holding an SQL transaction.
                await connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended($1,0))", str(session_id)
                )
                try:
                    if not await connection.fetchval(
                        "UPDATE whatsapp_signup_attempts SET consumed_at=now() "
                        "WHERE attempt_id=$1 AND session_id=$2 AND browser_hash=$3 "
                        "AND state_hash=$4 AND consumed_at IS NULL AND expires_at>now() "
                        "RETURNING attempt_id",
                        attempt_id,
                        session_id,
                        capability_hash(browser),
                        capability_hash(state),
                    ):
                        raise WhatsAppSignupError("signup_attempt_invalid_or_used")
                    token = await self.meta.exchange(code)
                    asset = await self.meta.verify_asset(
                        token, waba_id=waba_id, phone_number_id=phone_number_id
                    )
                    if not asset.is_on_biz_app:
                        raise WhatsAppSignupError("coexistence_business_app_number_required")
                    return await self._connect(connection, session_id, token, asset)
                finally:
                    await connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended($1,0))", str(session_id)
                    )
        except asyncpg.UniqueViolationError:
            raise WhatsAppSignupError("phone_already_assigned") from None

    async def cancel(self, session_id, browser, *, attempt_id, state):
        await self.require_browser(session_id, browser)
        async with self.repository.pool.acquire() as sql, sql.transaction():
            await sql.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", str(session_id)
            )
            attempt = await sql.fetchrow(
                "SELECT * FROM whatsapp_signup_attempts WHERE attempt_id=$1 AND session_id=$2 "
                "AND browser_hash=$3 AND state_hash=$4",
                attempt_id,
                session_id,
                capability_hash(browser),
                capability_hash(state),
            )
            if not attempt:
                raise WhatsAppSignupError("signup_attempt_invalid_or_used")
            if await sql.fetchval(
                "SELECT 1 FROM whatsapp_connections WHERE session_id=$1 "
                "AND status IN ('connected','active')",
                session_id,
            ):
                return {"locked": True}
            # A late cancellation must never unlock a newer signup attempt.
            if await sql.fetchval(
                "SELECT 1 FROM whatsapp_signup_attempts WHERE session_id=$1 AND created_at>$2",
                session_id,
                attempt["created_at"],
            ):
                return {"locked": True}
            await sql.execute(
                "UPDATE whatsapp_signup_attempts SET consumed_at=now() WHERE attempt_id=$1",
                attempt_id,
            )
            await sql.execute(
                "UPDATE whatsapp_archive_preferences SET locked=false WHERE session_id=$1",
                session_id,
            )
            return {"locked": False}

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
        preference = await sql.fetchrow(
            "SELECT * FROM whatsapp_archive_preferences WHERE session_id=$1", session_id
        )
        import_enabled = bool(preference and preference["import_enabled"])
        reference = f"whatsapp/{connection_id}"
        await sql.execute(
            "INSERT INTO whatsapp_connections(connection_id,session_id,waba_id,phone_number_id, "
            "display_number,credential_reference,coexistence) VALUES($1,$2,$3,$4,$5,$6,true) "
            "ON CONFLICT(session_id) DO UPDATE SET waba_id=EXCLUDED.waba_id, "
            "phone_number_id=EXCLUDED.phone_number_id,display_number=EXCLUDED.display_number, "
            "status='connecting',coexistence=true,error_code=NULL,updated_at=now()",
            connection_id,
            session_id,
            asset.waba_id,
            asset.phone_number_id,
            asset.display_number,
            reference,
        )
        await sql.execute(
            "UPDATE whatsapp_connections SET import_history_enabled=$2,archive_media_enabled=$2, "
            "drive_connection_id=$3 WHERE connection_id=$1",
            connection_id,
            import_enabled,
            preference["drive_id"] if import_enabled else None,
        )
        try:
            await self.credentials.write(
                connection_id,
                ConnectionCredentials(
                    access_token=token,
                ),
            )
            if not existing or not existing.subscribed_at:
                await self.meta.subscribe(token, asset.waba_id)
                await sql.execute(
                    "UPDATE whatsapp_connections SET subscribed_at=now() WHERE connection_id=$1",
                    connection_id,
                )
            if not existing or not existing.registered_at:
                # Meta's coexistence flow has already registered the number.
                # Never reset its PIN or call /register here.
                await sql.execute(
                    "UPDATE whatsapp_connections SET registered_at=now() WHERE connection_id=$1",
                    connection_id,
                )
            if import_enabled and (not existing or not existing.history_requested_at):
                await self.meta.request_history(token, asset.phone_number_id)
                await sql.execute(
                    "UPDATE whatsapp_connections SET history_requested_at=now(), "
                    "history_status=CASE WHEN history_status='not_requested' THEN 'requested' "
                    "ELSE history_status END WHERE connection_id=$1",
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
