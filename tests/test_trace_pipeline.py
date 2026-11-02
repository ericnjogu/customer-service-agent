"""Trace configuration checks and opt-in disposable collector/Tempo acceptance."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm required")


def render_collector(*args):
    result = subprocess.check_output(
        [
            "helm",
            "template",
            "collector",
            str(ROOT / "helm/observability-collector"),
            "-n",
            "monitoring",
            *args,
        ],
        text=True,
    )
    docs = [d for d in yaml.safe_load_all(result) if d]
    config = next(
        d for d in docs if d["metadata"]["name"] == "otel-collector" and d["kind"] == "ConfigMap"
    )
    return docs, yaml.safe_load(config["data"]["config.yaml"])


def test_production_trace_pipeline_and_datasource():
    docs, config = render_collector(
        "-f", str(ROOT / "helm/observability-collector/values-production.yaml")
    )
    assert config["service"]["pipelines"]["traces"]["exporters"] == ["otlphttp/traces"]
    assert config["exporters"]["otlphttp/traces"]["endpoint"] == "http://tempo.monitoring.svc:4318"
    assert "transform/trace_privacy" in config["service"]["pipelines"]["traces"]["processors"]
    ds = next(d for d in docs if d["metadata"]["name"] == "tempo-grafana-datasource")
    assert (
        yaml.safe_load(ds["data"]["tempo.yaml"])["datasources"][0]["url"]
        == "http://tempo.monitoring.svc:3200"
    )
    production = yaml.safe_load((ROOT / "helm/customer-service/values-production.yaml").read_text())
    assert production["telemetry"]["enabled"] is True
    assert float(production["telemetry"]["traceSampleRatio"]) == 0.1


def test_local_tracing_stays_optional_and_missing_endpoint_fails():
    docs, config = render_collector()
    assert config["service"]["pipelines"]["traces"]["exporters"] == ["debug"]
    assert not any(d["metadata"]["name"] == "tempo-grafana-datasource" for d in docs)
    with pytest.raises(subprocess.CalledProcessError):
        render_collector("--set", "exporters.traces.enabled=true")


def test_tempo_is_private_persistent_and_pinned():
    app = yaml.safe_load((ROOT / "gitops/production/tempo.yaml").read_text())
    source = app["spec"]["source"]
    assert source["targetRevision"] == "3.2.1"
    values = source["helm"]["valuesObject"]
    assert values["tempo"]["tag"] == "3.1.1"
    assert values["tempo"]["retention"] == "168h"
    assert values["persistence"]["enabled"]
    assert not values["persistence"]["enableStatefulSetAutoDeletePVC"]
    assert values["service"]["type"] == "ClusterIP"
    policy, scrape, rules = yaml.safe_load_all(
        (ROOT / "gitops/production/tempo-access.yaml").read_text()
    )
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert {p["port"] for r in policy["spec"]["ingress"] for p in r["ports"]} == {3200, 4318}
    assert all("podSelector" in peer for r in policy["spec"]["ingress"] for peer in r["from"])
    assert scrape["kind"] == "VMServiceScrape"
    assert scrape["spec"]["endpoints"] == [{"port": "tempo-prom-metrics", "path": "/metrics"}]
    assert scrape["spec"]["selector"]["matchExpressions"] == [
        {"key": "prometheus.io/service-monitor", "operator": "NotIn", "values": ["false"]}
    ]
    assert {r["alert"] for r in rules["spec"]["groups"][0]["rules"]} == {
        "TempoUnavailable",
        "TempoStorageNearlyFull",
    }


def test_request_reaches_tempo_with_sensitive_fields_removed(tmp_path):
    if os.getenv("AGENT_TRACE_CONTAINER_TESTS") != "true":
        pytest.skip("Set AGENT_TRACE_CONTAINER_TESTS=true for disposable Tempo/collector tests")
    runtime = os.getenv("AGENT_E2E_CONTAINER_RUNTIME", "nerdctl")
    prefix = "css-trace-test-" + uuid4().hex[:10]
    tempo, collector = prefix + "-tempo", prefix + "-collector"
    volume = prefix + "-storage"

    def run(*args, check=True):
        return subprocess.run(
            [runtime, *args], check=check, capture_output=True, text=True, timeout=180
        ).stdout.strip()

    app = yaml.safe_load((ROOT / "gitops/production/tempo.yaml").read_text())
    source = app["spec"]["source"]
    subprocess.run(
        [
            "helm",
            "pull",
            "tempo",
            "--repo",
            source["repoURL"],
            "--version",
            source["targetRevision"],
            "--untar",
            "--untardir",
            str(tmp_path),
        ],
        check=True,
        timeout=120,
    )
    values = tmp_path / "values.yaml"
    values.write_text(yaml.safe_dump(source["helm"]["valuesObject"]))
    subprocess.run(["helm", "lint", str(tmp_path / "tempo"), "-f", str(values)], check=True)
    rendered = subprocess.check_output(
        ["helm", "template", "tempo", str(tmp_path / "tempo"), "-f", str(values)], text=True
    )
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    services = [d for d in docs if d["kind"] == "Service"]
    assert all(d["spec"].get("type", "ClusterIP") == "ClusterIP" for d in services)
    scrape_targets = [
        d
        for d in services
        if d["metadata"].get("labels", {}).get("ristoh.co.ke/scrape") == "tempo"
        and d["metadata"].get("labels", {}).get("prometheus.io/service-monitor") != "false"
    ]
    assert len(scrape_targets) == 1
    _, scrape, _ = yaml.safe_load_all((ROOT / "gitops/production/tempo-access.yaml").read_text())
    metrics_port = scrape["spec"]["endpoints"][0]["port"]
    assert any(
        p["name"] == metrics_port and p["port"] == 3200 for p in scrape_targets[0]["spec"]["ports"]
    ), "The scrape port must match the rendered Service port, not the container port name"
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "tempo.yaml" in d.get("data", {}))
    mounts = []
    for filename, content in cm["data"].items():
        path = tmp_path / filename
        path.write_text(content)
        path.chmod(0o444)
        mounts += ["-v", f"{path}:/conf/{filename}:ro"]
    _, config = render_collector(
        "-f", str(ROOT / "helm/observability-collector/values-production.yaml")
    )
    # Only Kubernetes metadata enrichment is omitted in this standalone test.
    config.pop("extensions")
    config["receivers"] = {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}}
    pipeline = config["service"]["pipelines"]["traces"]
    pipeline["processors"].remove("k8sattributes")
    config["processors"] = {
        k: v for k, v in config["processors"].items() if k in pipeline["processors"]
    }
    config["exporters"] = {
        "otlphttp/traces": {
            **config["exporters"]["otlphttp/traces"],
            "endpoint": f"http://{tempo}:4318",
        }
    }
    config["service"] = {"pipelines": {"traces": pipeline}}
    path = tmp_path / "collector.yaml"
    path.write_text(yaml.safe_dump(config))
    path.chmod(0o444)
    collector_values = yaml.safe_load(
        (ROOT / "helm/observability-collector/values.yaml").read_text()
    )
    image = collector_values["image"]

    def endpoint(name, port):
        host_port = run("port", name, f"{port}/tcp").splitlines()[0].rsplit(":", 1)[1]
        return f"http://127.0.0.1:{host_port}"

    def wait_for(url, *, marker=None):
        for _ in range(90):
            try:
                response = httpx.get(url, headers={"Accept": "application/json"}, timeout=2)
                if response.status_code == 200 and (marker is None or marker in response.text):
                    return response
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        pytest.fail(
            "Trace endpoint did not become ready: "
            + url
            + "\n"
            + run("logs", "--tail", "25", tempo, check=False)
            + run("logs", "--tail", "25", collector, check=False)
        )

    try:
        run("network", "create", prefix)
        run("volume", "create", volume)
        # Root is test-only to initialize an empty Docker/nerdctl volume; production
        # runs as chart UID 10001 with Kubernetes fsGroup-managed PVC permissions.
        run(
            "run",
            "-d",
            "--name",
            tempo,
            "--network",
            prefix,
            "--user",
            "0",
            "-p",
            "127.0.0.1::3200",
            "-v",
            f"{volume}:/var/tempo",
            *mounts,
            "grafana/tempo:" + source["helm"]["valuesObject"]["tempo"]["tag"],
            "-config.file=/conf/tempo.yaml",
        )
        base = endpoint(tempo, 3200)
        wait_for(base + "/ready")
        run(
            "run",
            "-d",
            "--name",
            collector,
            "--network",
            prefix,
            "-p",
            "127.0.0.1::4318",
            "-v",
            f"{path}:/conf/collector.yaml:ro",
            image["repository"] + "@" + image["digest"],
            "--config=/conf/collector.yaml",
        )
        collector_url = endpoint(collector, 4318)
        # Empty OTLP requests probe readiness without creating test spans.
        for _ in range(60):
            try:
                if (
                    httpx.post(
                        collector_url + "/v1/traces", json={"resourceSpans": []}, timeout=2
                    ).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail(run("logs", "--tail", "30", collector, check=False))
        trace_id = uuid4().hex
        code = """
import sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from app.config import Settings
from app.observability import configure_tracing, shutdown_tracing
app = FastAPI()
@app.get("/trace-acceptance")
def probe():
    span = trace.get_current_span()
    span.set_attribute("acceptance.marker", "ristoh-trace-acceptance")
    span.set_attribute("http.url", "https://example.invalid?token=SYNTHETIC_URL_SECRET")
    span.set_attribute("db.statement", "SELECT SYNTHETIC_DB_SECRET")
    span.add_event("exception", {"exception.message": "SYNTHETIC_EXCEPTION_SECRET"})
    return {"ok": True}
provider = configure_tracing(app, Settings(
    telemetry_enabled=True, otel_exporter_otlp_endpoint=sys.argv[1],
    otel_service_name="trace-acceptance", deployment_environment="test"))
with TestClient(app) as client:
    headers = {"traceparent": "00-"+sys.argv[2]+"-0123456789abcdef-01"}
    assert client.get("/trace-acceptance", headers=headers).status_code == 200
shutdown_tracing(provider)
"""
        subprocess.run(
            [sys.executable, "-c", code, collector_url, trace_id], check=True, timeout=30
        )
        result = wait_for(base + "/api/traces/" + trace_id, marker="ristoh-trace-acceptance")
        data = json.dumps(result.json())
        assert "trace-acceptance" in data
        assert "SYNTHETIC_" not in data
        # Confirm WAL-backed traces survive a process restart with the same volume.
        run("restart", tempo)
        base = endpoint(tempo, 3200)
        wait_for(base + "/ready")
        wait_for(base + "/api/traces/" + trace_id, marker="ristoh-trace-acceptance")
    finally:
        for name in (collector, tempo):
            run("rm", "-f", name, check=False)
        run("volume", "rm", volume, check=False)
        run("network", "rm", prefix, check=False)
