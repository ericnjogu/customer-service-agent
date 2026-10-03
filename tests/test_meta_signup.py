import hashlib
import hmac
import logging

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.meta_signup import MetaSignupClient, MetaSignupError, verify_meta_signature


@pytest.fixture
def meta():
    state = {
        "is_valid": True,
        "app_id": "123",
        "expires_at": 0,
        "scopes": ["whatsapp_business_management", "whatsapp_business_messaging"],
        "granular_scopes": [{"scope": "whatsapp_business_management", "target_ids": ["456"]}],
    }
    phone = {
        "id": "789",
        "display_phone_number": "+254700000001",
        "code_verification_status": "VERIFIED",
    }
    requests = []

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("debug_token"):
            return httpx.Response(200, json={"data": state})
        if path.endswith("oauth/access_token"):
            return httpx.Response(200, json={"access_token": "customer-token"})
        if path.endswith("phone_numbers"):
            return httpx.Response(200, json={"data": [phone]})
        if path.endswith("/456"):
            return httpx.Response(200, json={"id": "456"})
        return httpx.Response(200, json={"success": True})

    client = MetaSignupClient(
        httpx.AsyncClient(
            base_url="https://graph.facebook.com", transport=httpx.MockTransport(handle)
        ),
        app_id="123",
        app_secret=SecretStr("app-secret"),
        version="v25.0",
    )
    return client, state, phone, requests


async def test_exchange_verify_subscribe_and_register(meta, caplog):
    client, _, _, requests = meta
    caplog.set_level(logging.DEBUG)
    token = await client.exchange(SecretStr("authorization-code"))
    asset = await client.verify_asset(token, waba_id="456", phone_number_id="789")
    assert asset.display_number == "+254700000001"
    await client.subscribe(token, asset.waba_id)
    await client.register(token, asset.phone_number_id, SecretStr("123456"))
    assert len(requests) == 6
    for secret in ("customer-token", "app-secret", "authorization-code", "123456"):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "change,code",
    [
        ({"is_valid": False}, "meta_token_invalid"),
        ({"app_id": "other"}, "meta_token_invalid"),
        ({"scopes": []}, "meta_token_invalid"),
        ({"expires_at": 1}, "meta_token_expired"),
        ({"data_access_expires_at": 1}, "meta_token_expired"),
        ({"granular_scopes": []}, "meta_waba_not_granted"),
    ],
)
async def test_token_validation(meta, change, code):
    client, state, _, _ = meta
    state.update(change)
    with pytest.raises(MetaSignupError, match=code):
        await client.verify_asset(SecretStr("token"), waba_id="456", phone_number_id="789")


@pytest.mark.parametrize(
    "change,code",
    [
        ({"id": "000"}, "meta_phone_not_in_waba"),
        ({"code_verification_status": "NOT_VERIFIED"}, "meta_phone_not_verified"),
    ],
)
async def test_phone_membership_and_verification(meta, change, code):
    client, _, phone, _ = meta
    phone.update(change)
    with pytest.raises(MetaSignupError, match=code):
        await client.verify_asset(SecretStr("token"), waba_id="456", phone_number_id="789")


async def test_http_error_is_sanitized():
    def fail(request):
        return httpx.Response(400, json={"error": "secret-provider-details"})

    async with httpx.AsyncClient(
        base_url="https://graph.facebook.com", transport=httpx.MockTransport(fail)
    ) as http:
        client = MetaSignupClient(
            http, app_id="123", app_secret=SecretStr("secret"), version="v25.0"
        )
        with pytest.raises(MetaSignupError, match="^meta_request_failed$"):
            await client.exchange(SecretStr("code"))


def test_signature_checks_exact_raw_body():
    body = b'{"entry": []}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(body, signature, "secret")
    assert not verify_meta_signature(body + b" ", signature, "secret")
    assert not verify_meta_signature(body, signature, "wrong")
    assert not verify_meta_signature(body, signature, "")
    assert not verify_meta_signature(body, None, "secret")
    assert not verify_meta_signature(body, "sha1=whatever", "secret")
