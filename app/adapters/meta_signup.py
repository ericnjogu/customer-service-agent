"""Server-side Meta verification. Treat SDK-provided asset IDs as untrusted."""

import hashlib
import hmac
import re
from dataclasses import dataclass
from time import time

import httpx
from pydantic import SecretStr

from app.adapters.secret_transport import secret_transport


class MetaSignupError(RuntimeError):
    """Only stable, sanitized codes may escape this adapter."""


@dataclass(frozen=True)
class VerifiedWhatsAppAsset:
    waba_id: str
    phone_number_id: str
    display_number: str
    is_on_biz_app: bool = False


def verify_meta_signature(body: bytes, signature: str | None, app_secret: str) -> bool:
    if not app_secret or not signature or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class MetaSignupClient:
    def __init__(
        self, client: httpx.AsyncClient, *, app_id: str, app_secret: SecretStr, version: str
    ):
        if not re.fullmatch(r"v\d+\.0", version):
            raise ValueError("A pinned Meta Graph API version is required")
        self.client = client
        self.app_id = app_id
        self.app_secret = app_secret
        self.version = version

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        # Do not log request/response payloads or HTTP exceptions; OAuth/debug APIs
        # contain secrets. HTTP client tracing must exclude this client's endpoint.
        try:
            with secret_transport():
                response = await self.client.request(method, f"/{self.version}/{path}", **kwargs)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or "error" in result:
                raise ValueError("Invalid Graph response")
            return result
        except (httpx.HTTPError, ValueError):
            raise MetaSignupError("meta_request_failed") from None

    async def exchange(self, code: SecretStr) -> SecretStr:
        result = await self._request(
            "POST",
            "oauth/access_token",
            data={
                "client_id": self.app_id,
                "client_secret": self.app_secret.get_secret_value(),
                "code": code.get_secret_value(),
            },
        )
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            raise MetaSignupError("meta_token_missing")
        return SecretStr(token)

    async def verify_asset(
        self, token: SecretStr, *, waba_id: str, phone_number_id: str
    ) -> VerifiedWhatsAppAsset:
        if not re.fullmatch(r"[0-9]+", waba_id) or not re.fullmatch(r"[0-9]+", phone_number_id):
            raise MetaSignupError("meta_asset_invalid")
        debug = await self._request(
            "GET",
            "debug_token",
            headers={"Authorization": f"Bearer {self.app_id}|{self.app_secret.get_secret_value()}"},
            params={"input_token": token.get_secret_value()},
        )
        data = debug.get("data", {})
        required = {"whatsapp_business_management", "whatsapp_business_messaging"}
        if not isinstance(data, dict) or not isinstance(data.get("scopes"), list):
            raise MetaSignupError("meta_token_invalid")
        if not all(isinstance(scope, str) for scope in data["scopes"]):
            raise MetaSignupError("meta_token_invalid")
        if (
            data.get("is_valid") is not True
            or str(data.get("app_id")) != self.app_id
            or not required.issubset(set(data.get("scopes", [])))
        ):
            raise MetaSignupError("meta_token_invalid")
        for field in ("expires_at", "data_access_expires_at"):
            expiry = data.get(field, 0)
            if not isinstance(expiry, (int, float)) or (expiry and expiry <= time()):
                raise MetaSignupError("meta_token_expired")
        # The selected WABA must be in this token's granted assets, not merely
        # accessible through some unrelated app-level administrator privilege.
        grants = data.get("granular_scopes")
        if not isinstance(grants, list):
            raise MetaSignupError("meta_waba_not_granted")
        targets = {
            str(target)
            for scope in grants
            if isinstance(scope, dict)
            and scope.get("scope") == "whatsapp_business_management"
            and isinstance(scope.get("target_ids"), list)
            for target in scope.get("target_ids", [])
        }
        if waba_id not in targets:
            raise MetaSignupError("meta_waba_not_granted")
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        account = await self._request("GET", waba_id, headers=headers, params={"fields": "id"})
        if str(account.get("id")) != waba_id:
            raise MetaSignupError("meta_waba_mismatch")
        cursor = None
        seen = set()
        for _ in range(20):
            params = {
                "fields": (
                    "id,display_phone_number,code_verification_status,is_on_biz_app,platform_type"
                ),
                "limit": "100",
            }
            if cursor:
                params["after"] = cursor
            phones = await self._request(
                "GET", f"{waba_id}/phone_numbers", headers=headers, params=params
            )
            rows = phones.get("data")
            if not isinstance(rows, list):
                raise MetaSignupError("meta_phone_invalid")
            for phone in rows:
                if not isinstance(phone, dict):
                    continue
                if str(phone.get("id")) == phone_number_id:
                    if phone.get("code_verification_status") != "VERIFIED":
                        raise MetaSignupError("meta_phone_not_verified")
                    display = phone.get("display_phone_number")
                    if not isinstance(display, str) or not display:
                        raise MetaSignupError("meta_phone_invalid")
                    return VerifiedWhatsAppAsset(
                        waba_id,
                        phone_number_id,
                        display,
                        phone.get("is_on_biz_app") is True
                        and phone.get("platform_type") == "CLOUD_API",
                    )
            paging = phones.get("paging", {})
            if not isinstance(paging, dict) or not isinstance(paging.get("cursors", {}), dict):
                raise MetaSignupError("meta_phone_invalid")
            cursor = paging.get("cursors", {}).get("after") if paging.get("next") else None
            if cursor is not None and not isinstance(cursor, str):
                raise MetaSignupError("meta_phone_invalid")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        raise MetaSignupError("meta_phone_not_in_waba")

    async def subscribe(self, token: SecretStr, waba_id: str) -> None:
        if not re.fullmatch(r"[0-9]+", waba_id):
            raise MetaSignupError("meta_asset_invalid")
        result = await self._request(
            "POST",
            f"{waba_id}/subscribed_apps",
            headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
            },
        )
        if result.get("success") is not True:
            raise MetaSignupError("meta_subscription_failed")

    async def register(self, token: SecretStr, phone_number_id: str, pin: SecretStr) -> None:
        if not re.fullmatch(r"[0-9]+", phone_number_id):
            raise MetaSignupError("meta_asset_invalid")
        if not re.fullmatch(r"[0-9]{6}", pin.get_secret_value()):
            raise ValueError("Registration PIN must have six digits")
        result = await self._request(
            "POST",
            f"{phone_number_id}/register",
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            json={"messaging_product": "whatsapp", "pin": pin.get_secret_value()},
        )
        if result.get("success") is not True:
            raise MetaSignupError("meta_registration_failed")

    async def request_history(self, token: SecretStr, phone_number_id: str) -> None:
        if not re.fullmatch(r"[0-9]+", phone_number_id):
            raise MetaSignupError("meta_asset_invalid")
        await self._request(
            "POST",
            f"{phone_number_id}/smb_app_data",
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            json={"sync_type": "history"},
        )
