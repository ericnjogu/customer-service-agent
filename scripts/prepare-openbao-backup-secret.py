#!/usr/bin/env python3
"""Re-encrypt the shared fil.one key for OpenBao without plaintext files/output."""

import os
import subprocess
import tempfile
from pathlib import Path

import yaml


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "gitops/production/secrets/production-secrets.enc.yaml"
    target = root / "infra/openbao/backup-credentials.enc.yaml"
    encrypted = list(yaml.safe_load_all(source.read_text()))
    recipients = {
        entry["recipient"]
        for document in encrypted
        for entry in document.get("sops", {}).get("age", [])
    }
    if not recipients:
        raise ValueError("No age recipients found in production SOPS metadata")
    decrypted = subprocess.run(
        ["sops", "decrypt", str(source)], capture_output=True, check=True, text=True
    ).stdout
    templates = {
        item["name"]: item
        for document in yaml.safe_load_all(decrypted)
        if document.get("metadata", {}).get("namespace") == "customer-service-production"
        for item in document.get("spec", {}).get("secretTemplates", [])
    }
    config = templates["app-configs"]["stringData"]
    required = (
        "AGENT_META_APP_ID",
        "AGENT_META_SIGNUP_CONFIGURATION_ID",
        "AGENT_META_APP_SECRET",
        "AGENT_META_WEBHOOK_VERIFICATION_TOKEN",
    )
    for name in required:
        value = str(config.get(name, "")).strip()
        if not value or value.startswith("replace-"):
            raise ValueError(f"Missing or placeholder app-configs entry: {name}")
    credentials = templates["backup-credentials"]["stringData"]
    for name in ("ACCESS_KEY_ID", "ACCESS_SECRET_KEY"):
        if not credentials.get(name) or str(credentials[name]).startswith("replace-"):
            raise ValueError(f"Missing or placeholder backup entry: {name}")
    document = {
        "apiVersion": "isindir.github.com/v1alpha3",
        "kind": "SopsSecret",
        "metadata": {"name": "openbao-backup", "namespace": "openbao"},
        "spec": {
            "suspend": False,
            "secretTemplates": [
                {
                    "name": "openbao-backup-s3",
                    "stringData": {
                        "AWS_ACCESS_KEY_ID": credentials["ACCESS_KEY_ID"],
                        "AWS_SECRET_ACCESS_KEY": credentials["ACCESS_SECRET_KEY"],
                    },
                }
            ],
        },
    }
    result = subprocess.run(
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
        input=yaml.safe_dump(document, sort_keys=False),
        capture_output=True,
        check=True,
        text=True,
    )
    # Only ciphertext reaches disk. Atomic replacement leaves the source untouched.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(result.stdout)
        os.replace(temporary, target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    print("Meta entries are populated (values not displayed; validity not tested).")
    print("Prepared infra/openbao/backup-credentials.enc.yaml using the existing fil.one key.")
    print("No cluster resources changed. Regenerate this file whenever the shared key rotates.")


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, OSError, KeyError, ValueError, yaml.YAMLError) as error:
        # Never emit captured SOPS output, decrypted YAML or credential values.
        message = str(error) if type(error) is ValueError else type(error).__name__
        raise SystemExit(f"Secret preparation failed: {message}") from None
