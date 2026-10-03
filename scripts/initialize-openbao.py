#!/usr/bin/env python3
"""Initialize the tunneled production vault once; keep all recovery output local."""

import base64
import json
import os
import ssl
import subprocess
from pathlib import Path

import httpx


def main():
    root = Path(__file__).resolve().parents[1]
    recovery = root / "local-docs/openbao-recovery"
    output = recovery / "initialization.json"
    ca = subprocess.run(
        [
            "kubectl",
            "-n",
            "openbao",
            "get",
            "secret",
            "openbao-tls",
            "-o",
            r"jsonpath={.data.ca\.crt}",
        ],
        capture_output=True,
        check=True,
    ).stdout
    certificate = base64.b64decode(ca).decode()
    context = ssl.create_default_context(cadata=certificate)
    port = int(os.getenv("OPENBAO_BOOTSTRAP_PORT", "18200"))
    with httpx.Client(base_url=f"https://localhost:{port}", verify=context, timeout=60) as client:
        status = client.get("/v1/sys/init")
        status.raise_for_status()
        if status.json()["initialized"]:
            if not output.exists():
                raise RuntimeError("Vault is initialized but local recovery output is absent")
            print("Vault already initialized; no initialization or key changes performed.")
            return
        if output.exists():
            raise RuntimeError("Local initialization output exists; refusing replacement")
        # Exclusive, owner-only destination is opened BEFORE creating recovery material.
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as destination:
            result = client.put(
                "/v1/sys/init",
                json={
                    "recovery_shares": 5,
                    "recovery_threshold": 3,
                },
            )
            result.raise_for_status()
            material = result.json()
            json.dump(material, destination)
            destination.flush()
            os.fsync(destination.fileno())
            # Persist the successful response before interpreting version-specific
            # field names: initialization cannot be repeated to recover lost output.
            if not material.get("root_token") or not (
                material.get("recovery_keys_base64")
                or material.get("recovery_keys")
                or material.get("keys_base64")
                or material.get("keys")
            ):
                raise RuntimeError("Unexpected initialization response saved for secure inspection")
    ca_path = recovery / "ca.crt"
    ca_path.write_text(certificate)
    os.chmod(ca_path, 0o600)
    print("Initialized once: five recovery shares, threshold three.")
    print("Recovery material saved privately in local-docs/openbao-recovery/initialization.json.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(
            f"Initialization stopped ({type(error).__name__}); no values displayed."
        ) from None
