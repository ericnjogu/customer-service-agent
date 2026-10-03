#!/usr/bin/env python3
"""Revoke only the initial bootstrap token after operator access is validated."""

import json
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main():
    recovery = Path(__file__).resolve().parents[1] / "local-docs/openbao-recovery"
    token = json.loads((recovery / "initialization.json").read_text())["root_token"]
    context = ssl.create_default_context(cafile=str(recovery / "ca.crt"))
    port = int(os.environ["OPENBAO_BOOTSTRAP_PORT"])
    with httpx.Client(
        base_url=f"https://localhost:{port}",
        verify=context,
        timeout=30,
        headers={"X-Vault-Token": token},
    ) as client:
        response = client.get("/v1/auth/token/lookup-self")
        if response.status_code != 403:
            response.raise_for_status()
            client.post("/v1/auth/token/revoke-self").raise_for_status()
        if client.get("/v1/auth/token/lookup-self").status_code != 403:
            raise RuntimeError("Initial root token revocation was not confirmed")
    marker = recovery / "bootstrap-root-revoked.txt"
    marker.write_text("Confirmed revoked: " + datetime.now(timezone.utc).isoformat() + "\n")
    marker.chmod(0o600)
    print("Initial root token revoked; subsequent lookup denied. Recovery shares retained.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(
            f"Revocation check failed ({type(error).__name__}); no values displayed."
        ) from None
