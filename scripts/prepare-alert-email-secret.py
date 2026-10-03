#!/usr/bin/env python3
"""Derive an encrypted Alertmanager config from existing Resend credentials."""

import subprocess
from pathlib import Path

import yaml


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "gitops/production/secrets/production-secrets.enc.yaml"
    documents = list(yaml.safe_load_all(source.read_text()))
    recipients = sorted({x["recipient"] for d in documents for x in d["sops"]["age"]})
    plaintext = subprocess.run(
        ["sops", "decrypt", str(source)], check=True, capture_output=True, text=True
    ).stdout
    templates = {
        item["name"]: item["stringData"]
        for doc in yaml.safe_load_all(plaintext)
        if doc["metadata"]["namespace"] == "customer-service-production"
        for item in doc["spec"]["secretTemplates"]
        if "stringData" in item
    }
    password = templates["api-keys"]["RESEND_API_KEY"]
    sender = templates["app-configs"]["AGENT_EMAIL_FROM"]
    if not password or not sender or str(password).startswith("replace-"):
        raise ValueError("Resend credentials or sender missing")
    config = {
        "global": {
            "smtp_smarthost": "smtp.resend.com:587",
            "smtp_from": sender,
            "smtp_auth_username": "resend",
            "smtp_auth_password": password,
            "smtp_require_tls": True,
        },
        "route": {
            "receiver": "discard",
            "group_by": ["alertname", "namespace"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
            "routes": [
                {"receiver": "operations-email", "matchers": ['severity=~"warning|critical"']}
            ],
        },
        "receivers": [
            {"name": "discard"},
            {
                "name": "operations-email",
                "email_configs": [{"to": "alerts@ristoh.co.ke", "send_resolved": True}],
            },
        ],
    }
    document = {
        "apiVersion": "isindir.github.com/v1alpha3",
        "kind": "SopsSecret",
        "metadata": {"name": "operations-alert-email", "namespace": "monitoring"},
        "spec": {
            "secretTemplates": [
                {
                    "name": "operations-alert-email",
                    "stringData": {
                        "alertmanager.yaml": yaml.safe_dump(config, sort_keys=False),
                    },
                }
            ]
        },
    }
    encrypted = subprocess.run(
        [
            "sops",
            "encrypt",
            "--age",
            ",".join(recipients),
            "--encrypted-suffix",
            "Templates",
            "--input-type",
            "yaml",
            "--output-type",
            "yaml",
            "/dev/stdin",
        ],
        input=yaml.safe_dump(document, sort_keys=False),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (root / "gitops/production/secrets/alert-email.enc.yaml").write_text(encrypted)
    print("Prepared encrypted operations email configuration; credentials not displayed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(
            f"Alert preparation failed ({type(error).__name__}); values hidden."
        ) from None
