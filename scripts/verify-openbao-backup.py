#!/usr/bin/env python3
"""Restore a fil.one snapshot only into a disposable localhost vault.

Requires the temporary bootstrap root still to be valid in the selected snapshot.
Never targets the production API, writes credentials to stdout, or changes S3.
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import yaml


def main():
    root = Path(__file__).resolve().parents[1]
    recovery = root / "local-docs/openbao-recovery"
    canary = os.environ["OPENBAO_RECOVERY_CANARY"]
    # Reject anything other than a connection UUID before building a vault path.
    from uuid import UUID

    canary = str(UUID(canary))
    decrypted = subprocess.run(
        ["sops", "decrypt", str(root / "infra/openbao/backup-credentials.enc.yaml")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    credentials = yaml.safe_load(decrypted)["spec"]["secretTemplates"][0]["stringData"]
    env = os.environ.copy()
    env.update(credentials)
    env.update(AWS_DEFAULT_REGION="eu-west-1", AWS_EC2_METADATA_DISABLED="true")
    aws = ["aws", "--endpoint-url", "https://eu-west-1.s3.filonecontent.com"]

    def command(args, **kwargs):
        return subprocess.run(args, check=True, capture_output=True, timeout=120, **kwargs).stdout

    objects = json.loads(
        command(
            aws
            + [
                "s3api",
                "list-objects-v2",
                "--bucket",
                "ristoh-css-postgres",
                "--prefix",
                "production/openbao/frequent/",
                "--output",
                "json",
            ],
            env=env,
        )
    )["Contents"]
    latest = max(
        (o for o in objects if o["Key"].endswith(".snapshot")), key=lambda o: o["LastModified"]
    )
    runtime = os.getenv("AGENT_E2E_CONTAINER_RUNTIME", "nerdctl")
    name = "css-openbao-restore-" + uuid4().hex[:12]
    started = False
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="openbao-restore-", dir=recovery) as folder:
        directory = Path(folder)
        snapshot = directory / "backup.snapshot"
        command(
            aws
            + [
                "s3api",
                "get-object",
                "--bucket",
                "ristoh-css-postgres",
                "--key",
                latest["Key"],
                str(snapshot),
            ],
            env=env,
        )
        os.chmod(snapshot, 0o600)
        config = directory / "server.hcl"
        config.write_text(
            "disable_mlock = true\n"
            'api_addr = "http://127.0.0.1:8200"\n'
            'cluster_addr = "http://127.0.0.1:8201"\n'
            'listener "tcp" { address = "0.0.0.0:8200" tls_disable = true }\n'
            'storage "raft" { path = "/tmp/raft" node_id = "isolated-restore" }\n'
            'seal "static" { current_key_id = "production-1" '
            'current_key = "file:///recovery-seal" }\n'
        )
        # No retry_join or application processes: the drill cannot join production
        # or send Meta traffic. Only a random loopback port is published.
        try:
            command(
                [
                    runtime,
                    "run",
                    "-d",
                    "--name",
                    name,
                    "-p",
                    "127.0.0.1::8200",
                    "-v",
                    f"{config}:/restore.hcl:ro",
                    "-v",
                    f"{recovery / 'seal-key-production-1'}:/recovery-seal:ro",
                    "openbao/openbao:2.7.1",
                    "server",
                    "-config=/restore.hcl",
                ]
            )
            started = True
            port = command([runtime, "port", name, "8200/tcp"]).decode().strip().split(":")[-1]
            with httpx.Client(base_url=f"http://127.0.0.1:{int(port)}", timeout=60) as client:

                def ready(initialized):
                    for _ in range(90):
                        try:
                            status = client.get("/v1/sys/health").json()
                            if status["initialized"] == initialized and not status.get(
                                "sealed", False
                            ):
                                return
                            if not initialized and not status["initialized"]:
                                return
                        except (httpx.HTTPError, ValueError, KeyError):
                            pass
                        time.sleep(1)
                    raise RuntimeError("Isolated vault readiness timed out")

                ready(False)
                response = client.put(
                    "/v1/sys/init", json={"recovery_shares": 1, "recovery_threshold": 1}
                )
                response.raise_for_status()
                client.headers["X-Vault-Token"] = response.json()["root_token"]
                ready(True)
                # Force applies ONLY to this newly created disposable vault.
                response = client.post(
                    "/v1/sys/storage/raft/snapshot-force",
                    content=snapshot.read_bytes(),
                    headers={"Content-Type": "application/octet-stream"},
                )
                response.raise_for_status()
                ready(True)
                client.headers["X-Vault-Token"] = json.loads(
                    (recovery / "initialization.json").read_text()
                )["root_token"]
                response = client.get("/v1/tenant-credentials/data/whatsapp/" + canary)
                response.raise_for_status()
                assert (
                    response.json()["data"]["data"]["access_token"] == "synthetic-recovery-canary"
                )
                print("PASS: fil.one snapshot restored and synthetic credential verified.")
                print(f"Recovery duration: {time.monotonic() - start:.1f} seconds")
                print(
                    "This verifies isolated data recovery; "
                    "not cross-cluster Kubernetes authentication."
                )
        finally:
            if started:
                command([runtime, "rm", "-f", "-v", name])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(
            f"Restore check failed ({type(error).__name__}); no credentials displayed."
        ) from None
