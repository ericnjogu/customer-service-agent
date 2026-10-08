"""Email proof for a new browser, separate from account verification and draft state."""

import hmac
import math
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.whatsapp_signup import browser_cookie, service_for
from app.config import get_settings
from app.onboarding_sessions import generate_verification_code, verification_code_hash
from app.verification_email import verification_email_html
from app.whatsapp_signup import capability_hash

router = APIRouter(prefix="/onboarding/sessions", tags=["browser-recovery"])


def context(request):
    settings = get_settings()
    public = urlsplit(settings.web_public_base_url)
    if request.headers.get("origin") != f"{public.scheme}://{public.netloc}":
        raise HTTPException(403, "Invalid onboarding request origin")
    return settings, service_for(request, require_enabled=False).repository.pool


def set_cookie(response, name, value, settings, age):
    response.set_cookie(
        name,
        value,
        httponly=True,
        samesite="strict",
        path="/",
        secure=settings.web_public_base_url.startswith("https://"),
        max_age=age,
    )


@router.post("/{session_id}/browser/send-code")
async def send_code(request: Request, response: Response, session_id: UUID):
    settings, pool = context(request)
    service = request.app.state.container.onboarding_sessions
    session = await service.get_session(session_id)
    if not session or not session.username_email_verified:
        raise HTTPException(409, "Complete initial account email verification first")
    code, browser = generate_verification_code(), secrets.token_urlsafe(32)
    async with pool.acquire() as sql, sql.transaction():
        await sql.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"browser:{session_id}"
        )
        previous = await sql.fetchrow(
            "SELECT * FROM onboarding_browser_challenges WHERE session_id=$1", session_id
        )
        now = datetime.now(timezone.utc)
        if previous and previous["resend_at"] > now:
            raise HTTPException(
                429,
                "Please wait before requesting another code",
                headers={
                    "Retry-After": str(
                        max(1, math.ceil((previous["resend_at"] - now).total_seconds()))
                    )
                },
            )
        await sql.execute(
            "INSERT INTO onboarding_browser_challenges"
            "(session_id,browser_hash,code_hash,expires_at,resend_at) "
            "VALUES($1,$2,$3,now()+interval '10 minutes',now()+interval '60 seconds') "
            "ON CONFLICT(session_id) DO UPDATE SET browser_hash=EXCLUDED.browser_hash, "
            "code_hash=EXCLUDED.code_hash,expires_at=EXCLUDED.expires_at, "
            "resend_at=EXCLUDED.resend_at, "
            "attempts=0,used_at=NULL",
            session_id,
            capability_hash(browser),
            verification_code_hash(
                f"browser:{session_id}:{code}", settings.onboarding_verification_code_secret
            ),
        )
    await service.email_sender.send_email(
        to=[str(session.admin.username_email)],
        subject="Verify this browser to resume onboarding",
        html=verification_email_html(
            name=session.admin.name,
            code=code,
            purpose="this browser",
            resume_url=f"{settings.web_public_base_url.rstrip('/')}?session_id={session_id}",
            ttl_minutes=10,
        ),
        text=(
            f"Enter {code} in the browser where you requested it to resume onboarding. "
            "It expires in 10 minutes."
        ),
    )
    set_cookie(response, f"onboarding-recovery-{session_id}", browser, settings, 600)
    return {"sent": True, "retry_after_seconds": 60}


class BrowserCode(BaseModel):
    code: str = Field(pattern=r"^[0-9]{6}$")


@router.post("/{session_id}/browser/verify-code")
async def verify_code(request: Request, response: Response, session_id: UUID, payload: BrowserCode):
    settings, pool = context(request)
    browser = request.cookies.get(f"onboarding-recovery-{session_id}", "")
    error = None
    token = secrets.token_urlsafe(32)
    async with pool.acquire() as sql, sql.transaction():
        row = await sql.fetchrow(
            "SELECT * FROM onboarding_browser_challenges WHERE session_id=$1 FOR UPDATE", session_id
        )
        if (
            not row
            or not browser
            or row["browser_hash"] != capability_hash(browser)
            or row["used_at"]
            or row["expires_at"] <= datetime.now(timezone.utc)
        ):
            error = HTTPException(
                422, "Code is invalid, expired, or already used. Request a new code."
            )
        elif row["attempts"] >= 5:
            error = HTTPException(
                429, "Too many attempts. Request a new code.", headers={"Retry-After": "60"}
            )
        elif not hmac.compare_digest(
            row["code_hash"],
            verification_code_hash(
                f"browser:{session_id}:{payload.code}", settings.onboarding_verification_code_secret
            ),
        ):
            await sql.execute(
                "UPDATE onboarding_browser_challenges SET attempts=attempts+1 WHERE session_id=$1",
                session_id,
            )
            error = HTTPException(
                429 if row["attempts"] == 4 else 422,
                "Incorrect verification code",
                headers={"Retry-After": "60"} if row["attempts"] == 4 else None,
            )
        else:
            result = await sql.fetchval(
                "INSERT INTO onboarding_browser_sessions(session_id,browser_hash,expires_at) "
                "SELECT session_id,$2,now()+interval '24 hours' FROM onboarding_sessions "
                "WHERE session_id=$1 AND username_email_verified "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "browser_hash=EXCLUDED.browser_hash,expires_at=EXCLUDED.expires_at "
                "RETURNING session_id",
                session_id,
                capability_hash(token),
            )
            if not result:
                raise HTTPException(409, "Complete initial account email verification first")
            await sql.execute(
                "UPDATE onboarding_browser_challenges SET used_at=now() WHERE session_id=$1",
                session_id,
            )
    if error:
        raise error
    set_cookie(response, browser_cookie(session_id), token, settings, 86400)
    response.delete_cookie(f"onboarding-recovery-{session_id}", path="/")
    return {"authorized": True}
