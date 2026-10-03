"""Opt-in disposable OpenBao tests; never point these at a production vault."""

import os
from time import monotonic
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.adapters.credential_store import (
    ConnectionCredentials,
    CredentialNotFound,
    CredentialStoreError,
    OpenBaoCredentialStore,
)


async def test_real_kv_policy_rotation_deletion_and_revocation(tmp_path):
    url = os.getenv("AGENT_OPENBAO_TEST_URL")
    root = os.getenv("AGENT_OPENBAO_TEST_ROOT_TOKEN")
    if not url or not root:
        pytest.skip(
            "Set AGENT_OPENBAO_TEST_URL and AGENT_OPENBAO_TEST_ROOT_TOKEN for disposable Bao"
        )
    mount = "test-" + uuid4().hex
    policy_name = mount
    async with httpx.AsyncClient(base_url=url, headers={"X-Vault-Token": root}) as admin:
        response = await admin.post(
            f"/v1/sys/mounts/{mount}",
            json={
                "type": "kv",
                "options": {"version": "2"},
            },
        )
        response.raise_for_status()
        try:
            policy = (
                f'path "{mount}/data/whatsapp/*" '
                '{ capabilities = ["create", "update", "read"] }\n'
                f'path "{mount}/metadata/whatsapp/*" {{ capabilities = ["read"] }}'
            )
            response = await admin.put(
                f"/v1/sys/policies/acl/{policy_name}", json={"policy": policy}
            )
            response.raise_for_status()
            response = await admin.post(
                "/v1/auth/token/create",
                json={
                    "policies": [policy_name],
                    "ttl": "5m",
                    "no_default_policy": True,
                },
            )
            response.raise_for_status()
            token = response.json()["auth"]["client_token"]
            async with httpx.AsyncClient(base_url=url) as client:
                store = OpenBaoCredentialStore(
                    client, role="unused", jwt_path=tmp_path / "unused", mount=mount
                )
                # This test exercises real KV/policies; Kubernetes auth requires a cluster
                # and is tested separately with mocked login/rotating projected JWTs.
                store._token = SecretStr(token)
                store._token_expires = monotonic() + 240
                key = uuid4()
                with pytest.raises(CredentialNotFound):
                    await store.read(key)
                await store.write(
                    key, ConnectionCredentials(access_token="first", registration_pin="123456")
                )
                assert (await store.read(key)).access_token.get_secret_value() == "first"
                response = await client.get("/v1/sys/mounts", headers={"X-Vault-Token": token})
                assert response.status_code == 403
                response = await client.get(
                    f"/v1/{mount}/data/unrelated/key", headers={"X-Vault-Token": token}
                )
                assert response.status_code == 403
                response = await admin.post(
                    f"/v1/{mount}/data/whatsapp/{key}",
                    json={
                        "data": {
                            "access_token": "rotated",
                            "registration_pin": "123456",
                        }
                    },
                )
                response.raise_for_status()
                assert (await store.read(key)).access_token.get_secret_value() == "rotated"
                response = await admin.delete(f"/v1/{mount}/data/whatsapp/{key}")
                response.raise_for_status()
                with pytest.raises(CredentialStoreError):
                    await store.read(key)
                await store.write(
                    key, ConnectionCredentials(access_token="restored", registration_pin="123456")
                )
                await store.read(key)
                response = await admin.post("/v1/auth/token/revoke", json={"token": token})
                response.raise_for_status()
                with pytest.raises(CredentialStoreError):
                    await store.read(key)
        finally:
            (await admin.delete(f"/v1/sys/mounts/{mount}")).raise_for_status()
            (await admin.delete(f"/v1/sys/policies/acl/{policy_name}")).raise_for_status()
