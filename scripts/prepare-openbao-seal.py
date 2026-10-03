#!/usr/bin/env python3
"""Create local recovery material and an encrypted seal manifest, never print keys."""

import base64
import os
import subprocess
import tempfile
from pathlib import Path

import yaml


def private_write(path, content):
    with path.open("xb") as output:
        os.chmod(path, 0o600)
        output.write(content)


def main():
    root = Path(__file__).resolve().parents[1]
    recovery = root / "local-docs/openbao-recovery"
    recovery.mkdir(mode=0o700, exist_ok=True)
    os.chmod(recovery, 0o700)
    key = recovery / "seal-key-production-1"
    if not key.exists():
        private_write(key, os.urandom(32))
    raw = key.read_bytes()
    if len(raw) != 32:
        raise ValueError("Existing seal key has incorrect length; refusing replacement")
    os.chmod(key, 0o600)
    age_source = Path(os.environ["SOPS_AGE_KEY_FILE"])
    age_backup = recovery / "production-age-key"
    if not age_backup.exists():
        private_write(age_backup, age_source.read_bytes())
    elif age_backup.read_bytes() != age_source.read_bytes():
        raise ValueError("Existing recovery age key differs; refusing overwrite")
    source = root / "gitops/production/secrets/production-secrets.enc.yaml"
    recipients = {
        item["recipient"]
        for doc in yaml.safe_load_all(source.read_text())
        for item in doc.get("sops", {}).get("age", [])
    }
    if not recipients:
        raise ValueError("Missing SOPS age recipients")
    manifest = {
        "apiVersion": "isindir.github.com/v1alpha3",
        "kind": "SopsSecret",
        "metadata": {"name": "openbao-seal", "namespace": "openbao"},
        "spec": {
            "suspend": False,
            "secretTemplates": [
                {
                    "name": "openbao-seal",
                    "data": {"key": base64.b64encode(raw).decode()},
                }
            ],
        },
    }
    encrypted = subprocess.run(
        [
            "sops",
            "encrypt",
            "--age",
            ",".join(sorted(recipients)),
            "--encrypted-suffix",
            "Templates",
            "--input-type",
            "yaml",
            "--output-type",
            "yaml",
            "/dev/stdin",
        ],
        input=yaml.safe_dump(manifest, sort_keys=False),
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    destination = root / "infra/openbao/seal.enc.yaml"
    with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as output:
        output.write(encrypted)
        temporary = output.name
    os.replace(temporary, destination)
    print("Recovery seal key and age-key copy prepared with owner-only permissions.")
    print("Encrypted seal manifest prepared; no key values displayed.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError, yaml.YAMLError):
        raise SystemExit(
            "Seal preparation failed; existing recovery material was preserved."
        ) from None
