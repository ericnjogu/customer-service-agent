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
        webhook_url="https://test.example/api/webhooks/whatsapp",
        webhook_verify_token=SecretStr("webhook-verification-secret"),
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
    for secret in (
        "customer-token", "app-secret", "authorization-code", "123456",
        "webhook-verification-secret",
    ):
        assert secret not in caplog.text


async def test_subscription_sets_environment_callback(meta):
    import json
    client, _, _, requests = meta
    await client.subscribe(SecretStr("token"), "456")
    assert requests[-1].url.path == "/v25.0/456/subscribed_apps"
    assert json.loads(requests[-1].content) == {
        "override_callback_uri": "https://test.example/api/webhooks/whatsapp",
        "verify_token": "webhook-verification-secret",
    }
    client.webhook_url = "https://changed.example/api/webhooks/whatsapp"
    await client.subscribe(SecretStr("token"), "456")
    assert json.loads(requests[-1].content)["override_callback_uri"] == client.webhook_url


@pytest.mark.parametrize("missing", ["webhook_url", "webhook_verify_token"])
async def test_missing_webhook_configuration_never_clears_override(meta, missing):
    client, _, _, requests = meta
    setattr(client, missing, None)
    with pytest.raises(MetaSignupError, match="meta_webhook_not_configured"):
        await client.subscribe(SecretStr("token"), "456")
    assert requests == []


async def test_redirect_exchange_and_discovery(meta):
    from urllib.parse import parse_qs

    client, _, _, requests = meta
    uri = "https://css.ristoh.co.ke/api/onboarding/whatsapp/callback"
    token = await client.exchange(SecretStr("code"), redirect_uri=uri)
    assert parse_qs(requests[0].content.decode())["redirect_uri"] == [uri]
    assert (await client.discover_asset(token)).phone_number_id == "789"


async def test_redirect_never_guesses_between_accounts(meta):
    client, state, _, requests = meta
    state["granular_scopes"][0]["target_ids"].append("999")
    with pytest.raises(MetaSignupError, match="meta_asset_selection_ambiguous"):
        await client.discover_asset(SecretStr("token"))
    assert len(requests) == 1


@pytest.mark.parametrize("extra", ["second_phone", "next_page"])
async def test_redirect_never_guesses_between_numbers(meta, extra):
    client, state, phone, _ = meta

    def handle(request):
        if request.url.path.endswith("debug_token"):
            return httpx.Response(200, json={"data": state})
        return httpx.Response(
            200,
            json={
                "data": [phone, {"id": "999"}] if extra == "second_phone" else [phone],
                "paging": {"next": "https://example.invalid"} if extra == "next_page" else {},
            },
        )

    client.client = httpx.AsyncClient(
        base_url="https://graph.facebook.com", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(MetaSignupError, match="meta_asset_selection_ambiguous"):
        await client.discover_asset(SecretStr("token"))


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


@pytest.mark.parametrize("status", ["VERIFIED", "NOT_VERIFIED", "PENDING", None])
async def test_coexistence_registration_does_not_require_sms_verification(meta, status):
    client, _, phone, _ = meta
    phone.update(is_on_biz_app=True, platform_type="CLOUD_API")
    if status is None:
        phone.pop("code_verification_status")
    else:
        phone["code_verification_status"] = status
    asset = await client.discover_asset(SecretStr("token"))
    assert asset.is_on_biz_app is True
    assert asset.platform_type == "CLOUD_API"
    assert asset.phone_number_id == "789"


@pytest.mark.parametrize("on_app,platform", [
    (False, "CLOUD_API"), (None, "CLOUD_API"), ("true", "CLOUD_API"),
    (True, None), (True, "NOT_APPLICABLE"), (True, "ON_PREMISE"),
])
async def test_incomplete_coexistence_flags_do_not_bypass_verification(meta, on_app, platform):
    client, _, phone, _ = meta
    phone.update(is_on_biz_app=on_app, platform_type=platform, code_verification_status="PENDING")
    with pytest.raises(MetaSignupError, match="meta_phone_not_verified"):
        await client.verify_asset(SecretStr("token"), waba_id="456", phone_number_id="789")


@pytest.mark.parametrize("failure", ["scope", "grant", "membership"])
async def test_coexistence_still_requires_permissions_and_ownership(meta, failure):
    client, state, phone, _ = meta
    phone.update(is_on_biz_app=True, platform_type="CLOUD_API", code_verification_status="PENDING")
    if failure == "scope":
        state["scopes"] = []
        expected = "meta_token_invalid"
    elif failure == "grant":
        state["granular_scopes"] = []
        expected = "meta_waba_not_granted"
    else:
        phone["id"] = "000"
        expected = "meta_phone_not_in_waba"
    with pytest.raises(MetaSignupError, match=expected):
        await client.verify_asset(SecretStr("token"), waba_id="456", phone_number_id="789")


async def test_http_error_is_sanitized(caplog):
    caplog.set_level(logging.DEBUG)
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
    assert "secret-provider-details" not in caplog.text
    assert "operation=code_exchange http_status=400" in caplog.text


async def test_meta_failure_logs_only_safe_diagnostics(caplog):
    caplog.set_level(logging.DEBUG)
    def fail(request):
        return httpx.Response(400, json={"error": {
            "message": "authorization-code customer-token app-secret",
            "code": 190, "error_subcode": 464, "fbtrace_id": "safe_Meta-trace123",
        }})

    async with httpx.AsyncClient(
        base_url="https://graph.facebook.com", transport=httpx.MockTransport(fail)
    ) as http:
        client = MetaSignupClient(
            http, app_id="123", app_secret=SecretStr("app-secret"), version="v25.0"
        )
        with pytest.raises(MetaSignupError):
            await client.discover_asset(SecretStr("customer-token"))
    assert "operation=token_validation http_status=400" in caplog.text
    assert "meta_code=190 meta_subcode=464 meta_trace_id=safe_Meta-trace123" in caplog.text
    for secret in ("authorization-code", "customer-token", "app-secret", "input_token"):
        assert secret not in caplog.text


async def test_transport_failure_does_not_log_exception_url(caplog):
    caplog.set_level(logging.DEBUG)
    def fail(request):
        raise httpx.ConnectError("https://example.com?access_token=secret-value", request=request)
    async with httpx.AsyncClient(
        base_url="https://graph.facebook.com", transport=httpx.MockTransport(fail)
    ) as http:
        client = MetaSignupClient(
            http, app_id="123", app_secret=SecretStr("app-secret"), version="v25.0"
        )
        with pytest.raises(MetaSignupError):
            await client.exchange(SecretStr("authorization-code"))
    assert "operation=code_exchange http_status=None" in caplog.text
    assert "secret-value" not in caplog.text


def test_signature_checks_exact_raw_body():
    body = b'{"entry": []}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_meta_signature(body, signature, "secret")
    assert not verify_meta_signature(body + b" ", signature, "secret")
    assert not verify_meta_signature(body, signature, "wrong")
    assert not verify_meta_signature(body, signature, "")
    assert not verify_meta_signature(body, None, "secret")
    assert not verify_meta_signature(body, "sha1=whatever", "secret")
