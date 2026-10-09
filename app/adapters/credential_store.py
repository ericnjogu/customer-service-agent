"""Private KV v2 access; credentials must never enter database/job payloads."""

import asyncio
from collections import OrderedDict
from pathlib import Path
from time import monotonic
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from app.adapters.secret_transport import secret_transport


class ConnectionCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: SecretStr
    registration_pin: SecretStr | None = None


class CredentialStoreError(RuntimeError):
    """Sanitized failure: never attach provider bodies or credential-bearing requests."""


class CredentialNotFound(CredentialStoreError):
    pass


class CredentialStore(Protocol):
    async def read(self, connection_id: UUID) -> ConnectionCredentials: ...

    async def write(self, connection_id: UUID, value: ConnectionCredentials) -> None: ...


class OpenBaoCredentialStore:
    """Kubernetes-authenticated, prefix-restricted KV v2 client.

    Reauthenticate with the current projected service-account JWT before token expiry.
    Cache entries are bounded and short-lived. Every read checks vault authorization
    and current KV version before using cached data: outage/revocation is fail-closed.
    The caller owns the HTTP client's lifetime and TLS configuration.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        role: str,
        jwt_path: Path,
        mount: str = "tenant-credentials",
        cache_size: int = 128,
        cache_ttl: float = 30,
    ) -> None:
        if not mount or not all(c.isalnum() or c in "-_" for c in mount):
            raise ValueError("OpenBao mount must be a single path segment")
        if cache_size < 1 or cache_ttl <= 0:
            raise ValueError("Credential cache limits must be positive")
        self.client = client
        self.role = role
        self.jwt_path = jwt_path
        self.mount = mount
        self.cache_size = cache_size
        self.cache_ttl = cache_ttl
        self._token = SecretStr("")
        self._token_expires = 0.0
        self._lock = asyncio.Lock()
        self._cache: OrderedDict[UUID, tuple[int, float, ConnectionCredentials]] = OrderedDict()

    def _path(self, kind: str, connection_id: UUID) -> str:
        # UUID coercion rejects traversal and arbitrary credential prefixes.
        return f"/v1/{self.mount}/{kind}/whatsapp/{UUID(str(connection_id))}"

    async def _authenticate(self) -> None:
        if self._token_expires > monotonic():
            return
        try:
            jwt = self.jwt_path.read_text().strip()
            if not jwt:
                raise ValueError("Missing service account token")
            with secret_transport():
                response = await self.client.post(
                    "/v1/auth/kubernetes/login", json={"role": self.role, "jwt": jwt}
                )
            response.raise_for_status()
            auth = response.json()["auth"]
            token = auth["client_token"]
            duration = int(auth["lease_duration"])
            if not isinstance(token, str) or not token or duration <= 0:
                raise ValueError("Invalid token lease")
            self._token = SecretStr(token)
            self._token_expires = monotonic() + duration * 0.8
        except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
            self._invalidate()
            raise CredentialStoreError("Credential-store authentication failed") from None

    def _invalidate(self) -> None:
        self._token = SecretStr("")
        self._token_expires = 0
        self._cache.clear()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        await self._authenticate()
        try:
            with secret_transport():
                response = await self.client.request(
                    method,
                    path,
                    headers={"X-Vault-Token": self._token.get_secret_value()},
                    **kwargs,
                )
            if response.status_code == 404:
                self._cache.clear()
                raise CredentialNotFound("Credential does not exist")
            response.raise_for_status()
            return response.json()
        except (ValueError, httpx.HTTPError):
            self._invalidate()
            raise CredentialStoreError(
                "Credential store is unavailable or access was denied"
            ) from None

    async def read(self, connection_id: UUID) -> ConnectionCredentials:
        async with self._lock:
            try:
                metadata = await self._request("GET", self._path("metadata", connection_id))
                version = int(metadata["data"]["current_version"])
                current = metadata["data"]["versions"][str(version)]
                if current.get("destroyed") or current.get("deletion_time"):
                    raise ValueError("Credential version deleted")
                cached = self._cache.get(connection_id)
                if cached and cached[0] == version and cached[1] > monotonic():
                    self._cache.move_to_end(connection_id)
                    return cached[2].model_copy(deep=True)
                result = await self._request("GET", self._path("data", connection_id))
                value = ConnectionCredentials.model_validate(result["data"]["data"])
                version = int(result["data"]["metadata"]["version"])
            except (ValueError, KeyError, TypeError):
                self._cache.pop(connection_id, None)
                raise CredentialStoreError(
                    "Credential store returned no usable credentials"
                ) from None
            self._cache[connection_id] = (version, monotonic() + self.cache_ttl, value)
            self._cache.move_to_end(connection_id)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            return value.model_copy(deep=True)

    async def write(self, connection_id: UUID, value: ConnectionCredentials) -> None:
        async with self._lock:
            self._cache.pop(connection_id, None)
            await self._request(
                "POST",
                self._path("data", connection_id),
                json={
                    "data": {
                        "access_token": value.access_token.get_secret_value(),
                        **(
                            {"registration_pin": value.registration_pin.get_secret_value()}
                            if value.registration_pin
                            else {}
                        ),
                    }
                },
            )

    async def write_drive_token(self, connection_id: UUID, refresh_token: SecretStr) -> None:
        async with self._lock:
            await self._request(
                "POST", f"/v1/{self.mount}/data/google-drive/{UUID(str(connection_id))}",
                json={"data": {"refresh_token": refresh_token.get_secret_value()}},
            )

    async def read_drive_token(self, connection_id: UUID) -> SecretStr:
        async with self._lock:
            result = await self._request(
                "GET", f"/v1/{self.mount}/data/google-drive/{UUID(str(connection_id))}"
            )
            try:
                token = result["data"]["data"]["refresh_token"]
                if not isinstance(token, str) or not token:
                    raise ValueError()
                return SecretStr(token)
            except (KeyError, TypeError, ValueError):
                raise CredentialStoreError("No usable Drive credentials") from None
