import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "local_bao_bootstrap", ROOT / "scripts/bootstrap-local-openbao.py"
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def options(**changes):
    return argparse.Namespace(
        **{
            "context": "rancher-desktop",
            "namespace": "customer-service",
            "service_account": "cs-local-customer-service-app",
            "role": "customer-service-cs-local",
            "meta_secret": "app-configs",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"context": "production"},
        {"namespace": "customer-service-production"},
        {"namespace": "openbao"},
        {"namespace": "openbao-local"},
        {"service_account": "../admin"},
        {"role": "root/*"},
    ],
)
def test_rejects_unsafe_targets(changes):
    with pytest.raises(ValueError):
        bootstrap.validate(options(**changes))


def test_local_target_and_private_material(tmp_path):
    bootstrap.validate(options())
    key = tmp_path / "seal"
    bootstrap.private_write(key, b"test-only")
    assert key.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        bootstrap.private_write(key, b"replacement")
    assert key.read_bytes() == b"test-only"


@pytest.mark.parametrize("existing", [False, True])
def test_configure_binds_only_local_service_account(existing):
    calls = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/v1/sys/mounts":
            return httpx.Response(
                200, json={"data": {"tenant-credentials/": {}} if existing else {}}
            )
        if request.url.path == "/v1/sys/auth":
            return httpx.Response(200, json={"data": {"kubernetes/": {}} if existing else {}})
        return httpx.Response(204)

    with httpx.Client(base_url="https://local.invalid", transport=httpx.MockTransport(handle)) as c:
        bootstrap.configure(c, "customer-service", "cs-local-customer-service-app", "local-role")
    role = next(body for _, path, body in calls if path.endswith("/role/local-role"))
    assert role["bound_service_account_names"] == ["cs-local-customer-service-app"]
    assert role["bound_service_account_namespaces"] == ["customer-service"]
    assert role["audience"] == "openbao"
    assert role["token_policies"] == ["customer-service"]
    assert role["token_max_ttl"] == "15m"
    creation = [
        path
        for method, path, _ in calls
        if method == "POST"
        and path in {"/v1/sys/auth/kubernetes", "/v1/sys/mounts/tenant-credentials"}
    ]
    assert len(creation) == (0 if existing else 2)


def test_local_chart_has_no_production_resources_or_peers():
    if not shutil.which("helm") or not (ROOT / "helm/openbao-platform/charts").exists():
        pytest.skip("Install Helm and run helm dependency build helm/openbao-platform")
    result = subprocess.run(
        [
            "helm",
            "template",
            "openbao-local",
            str(ROOT / "helm/openbao-platform"),
            "-n",
            "openbao-local",
            "-f",
            str(ROOT / "helm/openbao-platform/values-local.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = [r for r in yaml.safe_load_all(result.stdout) if r]
    stateful = [r for r in resources if r["kind"] == "StatefulSet"]
    assert len(stateful) == 1 and stateful[0]["spec"]["replicas"] == 1
    assert not any(
        r["kind"] in {"CronJob", "VMRule", "Certificate", "Issuer", "Ingress"} for r in resources
    )
    assert all(
        r["spec"].get("type", "ClusterIP") == "ClusterIP"
        for r in resources
        if r["kind"] == "Service"
    )
    assert 'current_key_id = "local-1"' in result.stdout
    assert "retry_join" not in result.stdout
    assert "production-1" not in result.stdout
    assert "filone" not in result.stdout
    assert "10.50.0." not in result.stdout
    assert "tls_disable = true" not in result.stdout
    assert "openbao-local-active.openbao-local.svc" in result.stdout


def test_deploy_script_flags_are_explicit():
    script = (ROOT / "scripts/deploy-local.sh").read_text()
    assert "config use-context" not in script
    assert 'command kubectl --context "${KUBE_CONTEXT}"' in script
    assert 'command helm --kube-context "${KUBE_CONTEXT}"' in script
    assert 'if [[ "${AGENT_ONBOARDING_WHATSAPP_ENABLED}" == "true" ]]' in script
    assert '"openbao.enabled=${AGENT_ONBOARDING_WHATSAPP_ENABLED}"' in script
    assert '"onboarding.whatsappEnabled=${AGENT_ONBOARDING_WHATSAPP_ENABLED}"' in script
