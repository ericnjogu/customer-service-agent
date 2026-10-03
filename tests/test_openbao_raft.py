"""Disposable single-node recovery mechanics; production quorum needs a cluster drill."""

import asyncio
import os
import subprocess
from uuid import uuid4

import httpx
import pytest

from app.adapters.secret_transport import secret_transport


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
            "-v",
            f"{tmp_path}:/test-config:ro",
            "openbao/openbao:2.7.1",
            "server",
            "-config=/test-config/server.hcl",
        )
        port = (await asyncio.to_thread(run, "port", name, "8200/tcp")).split(":")[-1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:

            async def ready(*, initialized):
                for _ in range(60):
                    try:
                        result = (await client.get("/v1/sys/health")).json()
                        if result["initialized"] == initialized and (
                            not initialized or not result["sealed"]
                        ):
                            return
                    except (httpx.HTTPError, ValueError, KeyError):
                        pass
                    await asyncio.sleep(0.5)
                pytest.fail("Disposable OpenBao did not become ready")

            await ready(initialized=False)
            with secret_transport():
                response = await client.put(
                    "/v1/sys/init",
                    json={"recovery_shares": 1, "recovery_threshold": 1},
                    timeout=30,
                )
                response.raise_for_status()
                client.headers["X-Vault-Token"] = response.json()["root_token"]
                await ready(initialized=True)
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
                await ready(initialized=True)
                assert (await client.get(path)).json()["data"]["data"]["value"] == "after"
                (
                    await client.post(
                        "/v1/sys/storage/raft/snapshot",
                        content=snapshot.content,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                ).raise_for_status()
                await ready(initialized=True)
                assert (await client.get(path)).json()["data"]["data"]["value"] == "before"
    finally:
        await asyncio.to_thread(run, "rm", "-f", "-v", name)
