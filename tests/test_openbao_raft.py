"""Disposable single-node recovery mechanics; production quorum needs a cluster drill."""

import asyncio
import os
import subprocess
from uuid import uuid4

import httpx
import pytest

from app.adapters.secret_transport import secret_transport


def _config_mounts(directory):
    # Only disposable synthetic test files are made readable. Keep pytest's
    # private host directory unchanged and mount each file independently so
    # container UID 100 need not traverse the runner-owned 0700 directory.
    mounts = []
    for name in ("server.hcl", "seal-key"):
        path = directory / name
        path.chmod(0o444)
        mounts.extend(["-v", f"{path}:/test-config/{name}:ro"])
    return mounts


def test_config_mounts_preserve_private_parent(tmp_path):
    tmp_path.chmod(0o700)
    for name in ("server.hcl", "seal-key"):
        (tmp_path / name).write_text("synthetic")
    mounts = _config_mounts(tmp_path)
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    for name in ("server.hcl", "seal-key"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o444
        assert f"{tmp_path / name}:/test-config/{name}:ro" in mounts
    assert f"{tmp_path}:/test-config:ro" not in mounts


async def _refresh_published_endpoint(client, run, name):
    # Docker may allocate a new ephemeral host port when restarting a container.
    mapping = await asyncio.to_thread(run, "port", name, "8200/tcp")
    port = int(mapping.splitlines()[0].rsplit(":", 1)[1])
    client.base_url = f"http://127.0.0.1:{port}"


async def test_refresh_published_endpoint_after_restart():
    mappings = iter(["127.0.0.1:41001", "127.0.0.1:41002"])
    requests = []

    def run(*args):
        assert args == ("port", "disposable-test", "8200/tcp")
        return next(mappings)

    def handle(request):
        requests.append(request.url.port)
        return httpx.Response(200, json={"initialized": True, "sealed": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await _refresh_published_endpoint(client, run, "disposable-test")
        await client.get("/v1/sys/health")
        await _refresh_published_endpoint(client, run, "disposable-test")
        await client.get("/v1/sys/health")
    assert requests == [41001, 41002]


async def test_static_auto_unseal_restart_and_snapshot_restore(tmp_path):
    if os.getenv("AGENT_OPENBAO_RAFT_TESTS") != "true":
        pytest.skip("Set AGENT_OPENBAO_RAFT_TESTS=true to start a disposable Raft container")
    runtime = os.getenv("AGENT_E2E_CONTAINER_RUNTIME", "nerdctl")
    name = "css-bao-raft-test-" + uuid4().hex[:12]
    # Test-only seal material, never a deployment secret or reusable backup key.
    (tmp_path / "seal-key").write_bytes(os.urandom(32))
    (tmp_path / "server.hcl").write_text(
        "disable_mlock = true\n"
        'api_addr = "http://127.0.0.1:8200"\n'
        'cluster_addr = "http://127.0.0.1:8201"\n'
        'listener "tcp" { address = "0.0.0.0:8200" tls_disable = true }\n'
        'storage "raft" { path = "/tmp/raft" node_id = "test" }\n'
        'seal "static" { current_key_id = "test" '
        'current_key = "file:///test-config/seal-key" }\n'
    )

    def run(*args):
        return subprocess.run(
            [runtime, *args], check=True, capture_output=True, text=True, timeout=60
        ).stdout.strip()

    try:
        await asyncio.to_thread(
            run,
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::8200",
            *_config_mounts(tmp_path),
            "openbao/openbao:2.7.1",
            "server",
            "-config=/test-config/server.hcl",
        )
        async with httpx.AsyncClient(timeout=5) as client:
            await _refresh_published_endpoint(client, run, name)

            async def ready(*, initialized, phase):
                last_probe = "No health response"
                state = "unknown"
                for attempt in range(60):
                    try:
                        result = (await client.get("/v1/sys/health")).json()
                        if result["initialized"] == initialized and (
                            not initialized or not result["sealed"]
                        ):
                            return
                        last_probe = (
                            f"initialized={result.get('initialized')}, "
                            f"sealed={result.get('sealed')}"
                        )
                    except (httpx.HTTPError, ValueError, KeyError) as error:
                        last_probe = type(error).__name__
                    if attempt % 5 == 0:
                        state = await asyncio.to_thread(
                            run,
                            "inspect",
                            "--format",
                            "{{.State.Status}} exit={{.State.ExitCode}}",
                            name,
                        )
                        if state.startswith(("exited", "dead")):
                            break
                    await asyncio.sleep(0.5)
                diagnostics = f"phase={phase}; container={state}; health={last_probe}"
                if not initialized:
                    # Before initialization there are no generated root tokens
                    # or recovery shares. Never dump post-initialization logs.
                    logs = await asyncio.to_thread(
                        subprocess.run,
                        [runtime, "logs", "--tail", "30", name],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    diagnostics += "\nStartup logs:\n" + logs.stdout + logs.stderr
                pytest.fail("Disposable OpenBao did not become ready: " + diagnostics)

            await ready(initialized=False, phase="startup")
            with secret_transport():
                response = await client.put(
                    "/v1/sys/init",
                    json={"recovery_shares": 1, "recovery_threshold": 1},
                    timeout=30,
                )
                response.raise_for_status()
                client.headers["X-Vault-Token"] = response.json()["root_token"]
                await ready(initialized=True, phase="initialization")
                (
                    await client.post(
                        "/v1/sys/mounts/test",
                        json={
                            "type": "kv",
                            "options": {"version": "2"},
                        },
                    )
                ).raise_for_status()
                path = "/v1/test/data/whatsapp/synthetic"
                (await client.post(path, json={"data": {"value": "before"}})).raise_for_status()
                snapshot = await client.get("/v1/sys/storage/raft/snapshot")
                snapshot.raise_for_status()
                assert len(snapshot.content) > 0
                (await client.post(path, json={"data": {"value": "after"}})).raise_for_status()
                await asyncio.to_thread(run, "restart", name)
                await _refresh_published_endpoint(client, run, name)
                await ready(initialized=True, phase="restart")
                assert (await client.get(path)).json()["data"]["data"]["value"] == "after"
                (
                    await client.post(
                        "/v1/sys/storage/raft/snapshot",
                        content=snapshot.content,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                ).raise_for_status()
                await ready(initialized=True, phase="restore")
                assert (await client.get(path)).json()["data"]["data"]["value"] == "before"
    finally:
        await asyncio.to_thread(run, "rm", "-f", "-v", name)
