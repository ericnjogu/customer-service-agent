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


def render_web(*args, release="trace-test"):
    result = subprocess.check_output(
        [
            "helm",
            "template",
            release,
            str(ROOT / "helm/customer-service"),
            "--set",
            "web.enabled=true",
            *args,
        ],
        text=True,
    )
    docs = [d for d in yaml.safe_load_all(result) if d]
    cm = next(d for d in docs if d["metadata"]["name"] == f"{release}-customer-service-web-nginx")
    return docs, cm["data"]["default.conf"]


@pytest.mark.parametrize("ratio", ["0", "0.1", "1"])
def test_web_trace_configuration_and_config_rollout(ratio):
    enabled = ["--set", "telemetry.enabled=true", "--set", f"telemetry.traceSampleRatio={ratio}"]
    docs, config = render_web(*enabled)
    assert "otel_trace_context propagate;" in config
    assert 'otel_span_name "web.api";' in config
    assert "$request_uri" not in config
    assert '"1" on;' in config and '"0" off;' in config
    assert "map $otel_parent_id $web_sampled" in config
    assert '"" $web_ratio_sampled;' in config
    assert "otel_trace off;" in config.split("location = /healthz", 1)[1].split("}", 1)[0]
    for callback in ["whatsapp", "drive"]:
        block = config.split(f"location = /api/onboarding/{callback}/callback", 1)[1].split("}", 1)[
            0
        ]
        assert "otel_trace off;" in block and "otel_trace_context ignore;" in block
    web = next(
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-web")
    )
    digest = web["spec"]["template"]["metadata"]["annotations"]["checksum/nginx"]
    assert len(digest) == 64
    disabled_docs, disabled = render_web()
    assert "otel_" not in disabled
    disabled_web = next(
        d
        for d in disabled_docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-web")
    )
    assert digest != disabled_web["spec"]["template"]["metadata"]["annotations"]["checksum/nginx"]


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
    assert production["telemetry"]["webEndpoint"] == "otel-collector.monitoring.svc:4317"
    local_setup = (ROOT / "scripts/deploy-local.sh").read_text()
    assert "telemetry.webEndpoint=otel-collector.${NAMESPACE}:4317" in local_setup


def test_local_tracing_stays_optional_and_missing_endpoint_fails():
    docs, config = render_collector()
    assert config["service"]["pipelines"]["traces"]["exporters"] == ["debug"]
    assert not any(d["metadata"]["name"] == "tempo-grafana-datasource" for d in docs)
    with pytest.raises(subprocess.CalledProcessError):
        render_collector("--set", "exporters.traces.enabled=true")


@pytest.mark.parametrize("web_parent", ["web-span", "wrong-parent", None])
def test_smoke_check_requires_connected_web_and_api_spans(tmp_path, web_parent):
    # No network/cluster calls: exercise the operational script against fixed OTLP.
    spans = [{"name": "GET /onboarding/config", "parentSpanId": web_parent}]
    if web_parent is not None:
        spans.append({"name": "web.api", "spanId": "web-span"})
    payload = json.dumps({"batches": [{"scopeSpans": [{"spans": spans}]}]})
    for name, content in {
        "curl": "exit 0",
        "kubectl": 'printf "%s" "$TRACE_TEST_PAYLOAD"',
        "sleep": "exit 0",
    }.items():
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\n" + content + "\n")
        executable.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/check-production-traces.sh")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TRACE_TEST_PAYLOAD": payload,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == (web_parent == "web-span")


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
    proxy, api, postgres = prefix + "-web", prefix + "-customer-service-app", prefix + "-postgres"
    app_image = prefix + "-app:local"
    volume = prefix + "-storage"

    def run(*args, check=True, timeout=180):
        try:
            return subprocess.run(
                [runtime, *args], check=check, capture_output=True, text=True, timeout=timeout
            ).stdout.strip()
        except subprocess.CalledProcessError as exc:
            pytest.fail(f"Container command failed: {args[:3]}\n{exc.stderr[-6000:]}")

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
    config["receivers"] = {
        "otlp": {
            "protocols": {
                "http": {"endpoint": "0.0.0.0:4318"},
                "grpc": {"endpoint": "0.0.0.0:4317"},
            }
        }
    }
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
        markers = [marker] if isinstance(marker, str) else (marker or [])
        for _ in range(90):
            try:
                response = httpx.get(url, headers={"Accept": "application/json"}, timeout=2)
                if response.status_code == 200 and all(m in response.text for m in markers):
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
            + run("logs", "--tail", "25", api, check=False)
            + run("logs", "--tail", "25", proxy, check=False)
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
        # Full chain: the actual rendered web proxy -> instrumented FastAPI ->
        # production business decorator -> real asyncpg -> collector -> Tempo.
        run("build", "-t", app_image, "-f", str(ROOT / "Dockerfile"), str(ROOT), timeout=600)
        run(
            "run",
            "-d",
            "--name",
            postgres,
            "--network",
            prefix,
            "-e",
            "POSTGRES_PASSWORD=trace-test-only",
            "postgres:16-alpine",
        )
        for _ in range(60):
            ready = (
                subprocess.run(
                    [runtime, "exec", postgres, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                    capture_output=True,
                ).returncode
                == 0
            )
            if ready:
                break
            time.sleep(0.5)
        assert ready
        probe = tmp_path / "probe.py"
        probe.write_text("""
import os
from contextlib import asynccontextmanager
import asyncpg
from fastapi import FastAPI
from opentelemetry import trace
from app.config import Settings
from app.observability import configure_tracing, shutdown_tracing, traced_operation

@asynccontextmanager
async def lifespan(app):
    app.state.db = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
    # Unparented polling must not create a DB-only trace.
    await app.state.db.fetchval("SELECT 1")
    yield
    await app.state.db.close()
    shutdown_tracing(provider)

app = FastAPI(lifespan=lifespan)
@app.get("/healthz")
def health():
    return {"ok": True}

@app.get("/trace-acceptance")
@traced_operation("acceptance.operation")
async def probe():
    await app.state.db.fetchval("SELECT 'SYNTHETIC_SQL_SECRET'")
    span = trace.get_current_span()
    span.set_attribute("acceptance.marker", "proxy-api-database")
    return {"trace_id": format(span.get_span_context().trace_id, "032x"),
            "sampled": bool(span.get_span_context().trace_flags.sampled)}

provider = configure_tracing(app, Settings(telemetry_enabled=True,
    otel_exporter_otlp_endpoint=os.environ["TEST_COLLECTOR_URL"],
    otel_service_name="trace-acceptance-api", issue_processing_enabled=False,
    onboarding_whatsapp_enabled=False))
""")
        probe.chmod(0o444)
        run(
            "run",
            "-d",
            "--name",
            api,
            "--network",
            prefix,
            "-v",
            f"{probe}:/app/probe.py:ro",
            "-e",
            f"TEST_DATABASE_URL=postgresql://postgres:trace-test-only@{postgres}/postgres",
            "-e",
            f"TEST_COLLECTOR_URL=http://{collector}:4318",
            app_image,
            "uvicorn",
            "probe:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        )
        _, web_config = render_web(
            "--set",
            "telemetry.enabled=true",
            "--set",
            "telemetry.traceSampleRatio=1",
            "--set",
            f"telemetry.webEndpoint={collector}:4317",
            release=prefix,
        )
        conf = tmp_path / "web.conf"
        conf.write_text(web_config)
        conf.chmod(0o444)
        nginx_image = next(
            line.split()[1]
            for line in (ROOT / "web/Dockerfile").read_text().splitlines()
            if line.startswith("FROM nginx:")
        )
        run(
            "run",
            "-d",
            "--name",
            proxy,
            "--network",
            prefix,
            "-p",
            "127.0.0.1::80",
            "-v",
            f"{ROOT / 'web/nginx-main.conf'}:/etc/nginx/nginx.conf:ro",
            "-v",
            f"{conf}:/etc/nginx/conf.d/default.conf:ro",
            nginx_image,
        )
        proxy_url = endpoint(proxy, 80)
        wait_for(proxy_url + "/healthz")
        response = wait_for(proxy_url + "/api/trace-acceptance?token=SYNTHETIC_URL_SECRET")
        full_trace_id = response.json()["trace_id"]
        assert response.json()["sampled"]
        full_trace = wait_for(
            base + "/api/traces/" + full_trace_id,
            marker=["web.api", "acceptance.operation", "SELECT"],
        ).json()
        spans = [
            s for b in full_trace["batches"] for ss in b.get("scopeSpans", []) for s in ss["spans"]
        ]
        by_name = {s["name"]: s for s in spans}
        assert {
            "web.api",
            "GET /trace-acceptance",
            "acceptance.operation",
            "SELECT",
        } <= by_name.keys()
        assert by_name["GET /trace-acceptance"]["parentSpanId"] == by_name["web.api"]["spanId"]
        assert (
            by_name["acceptance.operation"]["parentSpanId"]
            == by_name["GET /trace-acceptance"]["spanId"]
        )
        assert by_name["SELECT"]["parentSpanId"] == by_name["acceptance.operation"]["spanId"]
        assert "SYNTHETIC_" not in json.dumps(full_trace)
        unsampled_id = uuid4().hex
        response = httpx.get(
            proxy_url + "/api/trace-acceptance",
            headers={"traceparent": f"00-{unsampled_id}-0123456789abcdef-00"},
        )
        assert response.status_code == 200
        assert response.json() == {"trace_id": unsampled_id, "sampled": False}
        malformed = httpx.get(
            proxy_url + "/api/trace-acceptance", headers={"traceparent": "invalid-parent"}
        )
        assert malformed.status_code == 200 and malformed.json()["sampled"]
        # Validate production's ratio-based config and disabled mode with NGINX,
        # not just a YAML/text assertion. The upstream is on this test network.
        for index, extra in enumerate(
            [
                ["--set", "telemetry.enabled=false"],
                ["--set", "telemetry.enabled=true", "--set", "telemetry.traceSampleRatio=0.1"],
            ]
        ):
            _, candidate = render_web(
                *extra, "--set", f"telemetry.webEndpoint={collector}:4317", release=prefix
            )
            validation_config = tmp_path / f"validate-web-{index}.conf"
            validation_config.write_text(candidate)
            validation_config.chmod(0o444)
            run(
                "run",
                "--rm",
                "--network",
                prefix,
                "-v",
                f"{ROOT / 'web/nginx-main.conf'}:/etc/nginx/nginx.conf:ro",
                "-v",
                f"{validation_config}:/etc/nginx/conf.d/default.conf:ro",
                nginx_image,
                "nginx",
                "-t",
            )
        _, zero_ratio = render_web(
            "--set",
            "telemetry.enabled=true",
            "--set",
            "telemetry.traceSampleRatio=0",
            "--set",
            f"telemetry.webEndpoint={collector}:4317",
            release=prefix,
        )
        conf.chmod(0o644)
        conf.write_text(zero_ratio)
        conf.chmod(0o444)
        run("exec", proxy, "nginx", "-s", "reload")
        for _ in range(30):
            response = httpx.get(proxy_url + "/api/trace-acceptance")
            if response.status_code == 200 and not response.json()["sampled"]:
                break
            time.sleep(0.2)
        assert response.status_code == 200 and not response.json()["sampled"]
        sampled_id = uuid4().hex
        response = httpx.get(
            proxy_url + "/api/trace-acceptance",
            headers={"traceparent": f"00-{sampled_id}-0123456789abcdef-01"},
        )
        assert response.json() == {"trace_id": sampled_id, "sampled": True}
        # Collector failures must not block requests; export is asynchronous/bounded.
        run("stop", collector)
        assert httpx.get(proxy_url + "/api/trace-acceptance", timeout=5).status_code == 200
    finally:
        for name in (proxy, api, postgres, collector, tempo):
            run("rm", "-f", name, check=False)
        run("rmi", app_image, check=False)
        run("volume", "rm", volume, check=False)
        run("network", "rm", prefix, check=False)
