import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from app.api.onboarding_sessions import create_onboarding_session
from app.config import Settings
from app.turnstile import verify_signup_challenge


def settings(**overrides):
    return Settings(
        _env_file=None,
        onboarding_turnstile_enabled=True,
        turnstile_site_key="site-key",
        turnstile_secret_key=SecretStr("secret-key"),
        web_public_base_url="https://css.ristoh.co.ke",
        **overrides,
    )


@pytest.mark.parametrize("result", [
    {"success": False, "error-codes": ["timeout-or-duplicate"]},
    {"success": True, "hostname": "evil.example", "action": "onboarding"},
    {"success": True, "hostname": "css.ristoh.co.ke", "action": "other"},
    [],
])
def test_rejects_invalid_or_replayed_tokens(monkeypatch, result):
    install_response(monkeypatch, result)
    with pytest.raises(HTTPException) as error:
        asyncio.run(verify_signup_challenge(settings(), "token"))
    assert error.value.status_code == 400


def install_response(monkeypatch, result=None, unavailable=False):
    client = httpx.AsyncClient

    def handler(request):
        assert request.url == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        assert b"secret=secret-key" in request.content
        assert b"response=token" in request.content
        if unavailable:
            raise httpx.ConnectError("unavailable")
        return httpx.Response(200, json=result)

    monkeypatch.setattr("app.turnstile.httpx.AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))


def test_valid_signup_token(monkeypatch):
    install_response(monkeypatch, {
        "success": True, "hostname": "css.ristoh.co.ke", "action": "onboarding",
    })
    asyncio.run(verify_signup_challenge(settings(), "token"))


def test_provider_unavailable_fails_closed(monkeypatch):
    install_response(monkeypatch, unavailable=True)
    with pytest.raises(HTTPException) as error:
        asyncio.run(verify_signup_challenge(settings(), "token"))
    assert error.value.status_code == 503


def test_missing_token_blocks_api_before_session_or_email():
    service = SimpleNamespace(settings=settings(), create_session=AsyncMock())
    request = Request({"type": "http", "headers": [], "app": SimpleNamespace(
        state=SimpleNamespace(container=SimpleNamespace(onboarding_sessions=service)),
    )})
    with pytest.raises(HTTPException) as error:
        asyncio.run(create_onboarding_session(request, None))
    assert error.value.status_code == 400
    service.create_session.assert_not_awaited()


def test_disabled_local_flow_needs_no_token():
    config = settings()
    config.onboarding_turnstile_enabled = False
    asyncio.run(verify_signup_challenge(config, None))


def test_missing_keys_fail_closed():
    config = settings()
    config.turnstile_secret_key = None
    with pytest.raises(HTTPException) as error:
        asyncio.run(verify_signup_challenge(config, "token"))
    assert error.value.status_code == 503
