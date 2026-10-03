import json
from uuid import uuid4

import httpx
import pytest

from app.adapters.credential_store import (
    ConnectionCredentials,
    CredentialStoreError,
    OpenBaoCredentialStore,
)


@pytest.fixture
def vault(tmp_path):
    jwt = tmp_path / "jwt"
    jwt.write_text("projected-jwt")
    state = {"version": 1, "token": "tenant-token", "status": 200, "deleted": False}
    requests = []

    def handle(request):
        requests.append(request)
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": "DO-NOT-EXPOSE"})
        if request.url.path.endswith("/login"):
            assert json.loads(request.content) == {"role": "app", "jwt": jwt.read_text()}
            return httpx.Response(
                200,
                json={
                    "auth": {
                        "client_token": "bao-token",
                        "lease_duration": 60,
                    }
                },
            )
        assert request.headers["X-Vault-Token"] == "bao-token"
        if "/metadata/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "current_version": state["version"],
                        "versions": {
                            str(state["version"]): {
                                "destroyed": state["deleted"],
                                "deletion_time": "",
                            }
                        },
                    }
                },
            )
        if request.method == "POST":
            state["token"] = json.loads(request.content)["data"]["access_token"]
            state["version"] += 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "data": {"access_token": state["token"], "registration_pin": "123456"},
                    "metadata": {"version": state["version"]},
                }
            },
        )

    client = httpx.AsyncClient(base_url="https://vault.test", transport=httpx.MockTransport(handle))
    store = OpenBaoCredentialStore(client, role="app", jwt_path=jwt, cache_size=2)
    return store, state, requests, jwt


async def test_cache_is_bounded_and_checks_authorization_on_every_read(vault):
    store, state, requests, _ = vault
    connection = uuid4()
    await store.read(connection)
    await store.read(connection)
    assert sum("/data/" in r.url.path for r in requests) == 1
    assert sum("/metadata/" in r.url.path for r in requests) == 2
    await store.read(uuid4())
    await store.read(uuid4())
    assert len(store._cache) == 2
    state["status"] = 403
    with pytest.raises(CredentialStoreError, match="unavailable or access was denied"):
        await store.read(connection)
    assert not store._cache


async def test_updates_and_external_rotation_invalidate_cached_values(vault):
    store, state, _, _ = vault
    connection = uuid4()
    await store.read(connection)
    await store.write(
        connection,
        ConnectionCredentials(
            access_token="updated",
            registration_pin="654321",
        ),
    )
    assert (await store.read(connection)).access_token.get_secret_value() == "updated"
    state["version"] += 1
    state["token"] = "external-rotation"
    assert (await store.read(connection)).access_token.get_secret_value() == "external-rotation"


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_outages_do_not_serve_cached_credentials(vault, status):
    store, state, _, _ = vault
    connection = uuid4()
    await store.read(connection)
    state["status"] = status
    with pytest.raises(CredentialStoreError) as error:
        await store.read(connection)
    assert "DO-NOT-EXPOSE" not in str(error.value)
    assert not store._cache


async def test_deleted_credentials_are_not_served_from_cache(vault):
    store, state, _, _ = vault
    connection = uuid4()
    await store.read(connection)
    state["deleted"] = True
    with pytest.raises(CredentialStoreError):
        await store.read(connection)


async def test_expired_auth_rereads_projected_jwt(vault):
    store, _, requests, jwt = vault
    await store.read(uuid4())
    store._token_expires = 0
    jwt.write_text("rotated-jwt")
    await store.read(uuid4())
    assert sum(r.url.path.endswith("/login") for r in requests) == 2


def test_credentials_repr_is_redacted():
    value = ConnectionCredentials(access_token="secret-token", registration_pin="123456")
    assert "secret-token" not in repr(value)
    assert "123456" not in repr(value)
