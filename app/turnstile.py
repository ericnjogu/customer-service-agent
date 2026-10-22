"""Verify signup challenges before any session or email side effects."""

from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from app.config import Settings


async def verify_signup_challenge(settings: Settings, token: str | None) -> None:
    if not settings.onboarding_turnstile_enabled:
        return
    if not settings.turnstile_site_key or not settings.turnstile_secret_key:
        raise HTTPException(503, "Signup verification is not configured. Please try later.")
    if not token or len(token) > 2048:
        raise HTTPException(400, "Complete the security verification before continuing.")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": settings.turnstile_secret_key.get_secret_value(),
                    "response": token,
                },
            )
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Security verification is unavailable. Please retry.") from None
    if (
        not isinstance(result, dict)
        or result.get("success") is not True
        or result.get("action") != "onboarding"
        or result.get("hostname") != urlsplit(settings.web_public_base_url).hostname
    ):
        raise HTTPException(400, "Security verification failed or expired. Please retry.")
