"""Regression checks for the k3s/VictoriaMetrics production monitoring contract."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def monitoring_values():
    apps = yaml.safe_load_all((ROOT / "gitops/production/observability.yaml").read_text())
    return next(a for a in apps if a["metadata"]["name"] == "victoria-metrics")["spec"]["source"][
        "helm"
    ]["valuesObject"]


def test_k3s_reuses_api_scrape_without_disabling_component_rules():
    values = monitoring_values()
    assert values["kubeScheduler"]["enabled"] is False
    assert values["kubeControllerManager"]["enabled"] is False
    for group, old_job in (
        ("kube-scheduler.rules", "kube-scheduler"),
        ("kubernetes-system-scheduler", "kube-scheduler"),
        ("kubernetes-system-controller-manager", "kube-controller-manager"),
    ):
        rule_group = values["defaultRules"]["groups"][group]
        assert rule_group["enabled"] is True
        assert rule_group["labelRewrites"]["job"] == {
            "match": old_job,
            "value": "apiserver",
        }


def test_etcd_scrapes_all_private_members_without_database_credentials():
    etcd = monitoring_values()["kubeEtcd"]
    assert etcd.get("enabled", True) is True
    assert etcd["endpoints"] == ["10.50.0.11", "10.50.0.12", "10.50.0.13"]
    assert etcd["service"] == {"port": 2381, "targetPort": 2381}
    assert etcd["vmScrape"]["spec"]["endpoints"] == [{"port": "http-metrics", "scheme": "http"}]


def test_no_data_alert_only_exempts_measured_scheduler_idle_periods():
    expression = monitoring_values()["defaultRules"]["rules"]["RecordingRulesNoData"]["spec"][
        "expr"
    ]
    assert 'group!="kube-scheduler.rules"' in expression
    assert 'group="kube-scheduler.rules"' in expression
    assert "unless on()" in expression
    assert 'scheduler_scheduling_attempt_duration_seconds_count{job="apiserver"}' in expression
    assert "== 0" in expression
    assert "or vector(0)" not in expression  # missing input must not be treated as idle


def test_etcd_listener_is_private_and_restart_waits_for_readiness():
    tasks = yaml.safe_load((ROOT / "ansible/roles/k3s/tasks/main.yml").read_text())
    metrics = next(t for t in tasks if t.get("register") == "k3s_metrics_configuration")
    config = yaml.safe_load(metrics["ansible.builtin.copy"]["content"])
    assert config == {"etcd-expose-metrics": True}
    installs = [t for t in tasks if "INSTALL_K3S_EXEC" in t.get("environment", {})]
    assert len(installs) == 2
    assert all(
        "--node-ip={{ wireguard_address }}" in t["environment"]["INSTALL_K3S_EXEC"]
        for t in installs
    )
    restart = next(t for t in tasks if "ansible.builtin.systemd" in t)
    assert "k3s_metrics_configuration.changed" in " ".join(restart["when"])
    ready = next(t for t in tasks if t.get("register") == "k3s_ready")
    assert tasks.index(ready) > tasks.index(restart)
    assert "--raw=/readyz" in ready["ansible.builtin.command"]["argv"]
    assert "k3s_ready.rc == 0" in ready["until"]
    play = yaml.safe_load((ROOT / "ansible/site.yml").read_text())[0]
    assert play["serial"] == 1


def test_rendered_postgres_scrape_and_backup_alerts():
    helm = shutil.which("helm")
    if not helm:
        pytest.skip("Helm is required to validate the production chart")
    result = subprocess.run(
        [helm, "template", "production-platform", str(ROOT / "helm/production-platform")],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = list(yaml.safe_load_all(result.stdout))
    cluster = next(r for r in resources if r and r["kind"] == "Cluster")
    assert cluster["spec"]["monitoring"]["enablePodMonitor"] is False
    scrape = next(r for r in resources if r and r["kind"] == "VMPodScrape")
    assert scrape["metadata"]["namespace"] == "monitoring"
    assert scrape["spec"]["namespaceSelector"]["matchNames"] == [cluster["metadata"]["namespace"]]
    assert scrape["spec"]["selector"]["matchLabels"] == {
        "cnpg.io/cluster": cluster["metadata"]["name"]
    }
    assert scrape["spec"]["podMetricsEndpoints"] == [
        {"port": "metrics", "path": "/metrics", "scheme": "http", "interval": "30s"}
    ]
    rules = next(r for r in resources if r and r["kind"] == "VMRule")
    backups = next(g for g in rules["spec"]["groups"] if g["name"] == "production-backups")
    assert {r["alert"] for r in backups["rules"]} == {
        "ProductionPostgresBackupFailed",
        "ProductionPostgresBaseBackupStale",
        "ProductionBackupMetricsMissing",
    }
    assert all("barman_cloud_cloudnative_pg_io_" in r["expr"] for r in backups["rules"])
