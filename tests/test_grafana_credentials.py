from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_grafana_uses_stable_admin_secret_and_preserves_storage():
    applications = yaml.safe_load_all(
        (ROOT / "gitops/production/observability.yaml").read_text()
    )
    application = next(
        item for item in applications if item["metadata"]["name"] == "victoria-metrics"
    )
    grafana = application["spec"]["source"]["helm"]["valuesObject"]["grafana"]
    assert grafana["admin"] == {
        "existingSecret": "grafana-admin",
        "userKey": "admin-user",
        "passwordKey": "admin-password",
    }
    assert "adminPassword" not in grafana
    assert grafana["persistence"]["enabled"] is True


def test_grafana_admin_manifest_encrypts_all_template_values():
    secret = yaml.safe_load(
        (ROOT / "gitops/production/secrets/grafana-admin.enc.yaml").read_text()
    )
    assert secret["kind"] == "SopsSecret"
    assert secret["metadata"]["name"] == "grafana-admin"
    assert secret["metadata"]["namespace"] == "monitoring"
    assert secret["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"
    assert secret["sops"]["encrypted_suffix"] == "Templates"
    template = secret["spec"]["secretTemplates"][0]
    assert template["name"].startswith("ENC[AES256_GCM,")
    assert set(template["stringData"]) == {"admin-user", "admin-password"}
    assert all(value.startswith("ENC[AES256_GCM,") for value in template["stringData"].values())
    reference = yaml.safe_load(
        (ROOT / "gitops/production/secrets/alert-email.enc.yaml").read_text()
    )
    assert {entry["recipient"] for entry in secret["sops"]["age"]} == {
        entry["recipient"] for entry in reference["sops"]["age"]
    }
