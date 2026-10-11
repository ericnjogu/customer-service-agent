import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm/observability-collector"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm required")


def render(chart=CHART):
    output = subprocess.check_output(
        [
            "helm",
            "template",
            "observability-collector",
            str(chart),
            "-n",
            "monitoring",
            "-f",
            str(chart / "values-production.yaml"),
        ],
        text=True,
    )
    objects = list(yaml.safe_load_all(output))
    config = next(
        item
        for item in objects
        if item["kind"] == "ConfigMap" and item["metadata"]["name"] == "otel-collector"
    )
    daemon = next(item for item in objects if item["kind"] == "DaemonSet")
    return yaml.safe_load(config["data"]["config.yaml"]), daemon


def test_collector_identity_and_endpoint():
    config, _ = render()
    associations = config["processors"]["k8sattributes"]["pod_association"]
    assert associations[0]["sources"] == [{"from": "resource_attribute", "name": "k8s.pod.uid"}]
    endpoint = config["exporters"]["otlphttp/metrics"]["endpoint"]
    assert "vmsingle-vm-victoria-metrics-k8s-stack" in endpoint
    for pipeline in config["service"]["pipelines"].values():
        processors = pipeline["processors"]
        assert processors.index("k8sattributes") < processors.index("transform/service_name")
        assert processors.index("transform/service_name") < processors.index("resource/common")


def test_template_only_change_triggers_rollout(tmp_path):
    chart = tmp_path / "chart"
    shutil.copytree(CHART, chart)
    _, before = render(chart)
    template = chart / "templates/resources.yaml"
    template.write_text(template.read_text().replace("batch: {}", "batch: {timeout: 2s}"))
    _, after = render(chart)

    def checksum(daemon):
        return daemon["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

    assert checksum(before) != checksum(after)
    assert len(checksum(after)) == hashlib.sha256().digest_size * 2


def test_canary_service_label_retains_pod_identity():
    apps = list(yaml.safe_load_all((ROOT / "gitops/production/observability.yaml").read_text()))
    loki = next(app for app in apps if app["metadata"]["name"] == "loki")
    args = loki["spec"]["source"]["helm"]["valuesObject"]["lokiCanary"]["extraArgs"]
    assert args == ["-streamname=service_name", "-streamvalue=loki-canary"]
