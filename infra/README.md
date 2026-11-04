# Production infrastructure

The production platform consists of three one.com Cloud Server M instances and a
private fil.one S3-compatible bucket. OpenTofu manages three proxied Cloudflare DNS
records for round-robin origin selection. Ansible configures the hosts, WireGuard and
k3s. Argo CD manages Kubernetes workloads.

## Inputs that must remain outside Git

- Three public server addresses and verified SSH host keys.
- Per-node WireGuard private keys and the shared k3s token.
- The operator CIDR.
- Cloudflare API token with DNS edit access and the zone ID.
- fil.one access key, secret key, and a Restic repository password.
- Production age private key and application secret values.

Copy `ansible/inventory/production.example.yml` to the ignored
`ansible/inventory/production.yml` and encrypt its secret variables with
`ansible-vault`. Copy `infra/production/production.auto.tfvars.example` to an
untracked `.auto.tfvars` file and export the Cloudflare token:

```bash
export TF_VAR_cloudflare_api_token='...'
tofu -chdir=infra/production init -backend-config=backend.hcl
tofu -chdir=infra/production plan -out=production.tfplan
tofu -chdir=infra/production apply production.tfplan
```

The example backend deliberately uses an ignored local state path. Store an
encrypted copy of state with the recovery material; it contains origin addresses
and Cloudflare identifiers but no token.

## Bootstrap order

1. Verify the provisioned Ubuntu 26.04 hosts and add their SSH host fingerprints locally.
2. Generate unique WireGuard and age keys; store recovery copies independently.
3. Install the pinned Ansible version used by CI.
4. Run `ansible-playbook ansible/site.yml --ask-vault-pass` twice; the second run
   must report no unexpected changes.
5. Copy `/etc/rancher/k3s/k3s.yaml` from the first server and keep it in an ignored,
   mode-`0600` file. Keep the Kubernetes API private: either replace its server address
   with a WireGuard address on an operator device connected to that mesh, or use
   `https://127.0.0.1:6443` while an SSH tunnel is active:

   ```bash
   ssh -f -N -i /path/to/operator-key \
     -L 127.0.0.1:6443:10.50.0.11:6443 administrator@ORIGIN_1_PUBLIC_IP
   export KUBECONFIG="$PWD/local-docs/production-kubeconfig"
   ```

   Do not expose TCP 6443 publicly. Copy the full CA-bound server token from
   `/var/lib/rancher/k3s/server/token` into separately protected recovery material.
   SSH is allowlisted to `operator_cidr`. Before moving to a network with a new
   public address, update that CIDR and rerun the `common` role. If access is
   already blocked, use the one.com browser/serial console to add the new `/32`;
   do not temporarily open SSH to the internet.
6. Export the fil.one and Restic variables required by
   `scripts/init-fil-one-backups.sh`, initialize the encrypted cluster-state
   repository, then verify it with `scripts/check-production-backups.sh`.
7. Merge the deployment configuration so GitHub Actions creates `deploy/production`
   with real GHCR digests.
8. Install Helm 3, set `SOPS_AGE_KEY_FILE`, and run
   `scripts/bootstrap-production.sh`. Helm 4 is intentionally rejected because the
   pinned Argo CD chart has been validated with Helm 3. If both are installed, set
   `HELM_BIN` to the Helm 3 binary.
9. Commit the encrypted `SopsSecret`, then wait for Argo applications to become
   `Synced` and `Healthy`.
10. Confirm every origin directly with
    `curl --resolve css.ristoh.co.ke:443:ORIGIN_IP https://css.ristoh.co.ke/api/healthz`.
11. Apply the Cloudflare DNS plan only after all three direct checks succeed.

## Networking

k3s uses WireGuard addresses for the Kubernetes API, embedded etcd and Flannel.
UFW permits public TCP 80/443, WireGuard UDP between the three origins, and SSH only
from the operator CIDR. All cluster ports are accepted only on `wg0`. The bundled k3s
ServiceLB publishes Traefik on every node. Cloudflare proxies three same-name `A`
records and can select any origin, but this initial DNS-only configuration does not
perform active health checks or automatically remove a failed origin. Upgrade the
OpenTofu stack to Cloudflare Load Balancing before treating origin failover as automatic.

## Database and recovery

CloudNativePG keeps three local-storage PostgreSQL instances on distinct nodes. Local
volumes are not independently durable; quorum replication and the off-cluster copy
are both required. The fil.one bucket is never mounted as the live database filesystem.

The official Barman Cloud CNPG-I plugin continuously archives WAL files and creates
a daily physical base backup under `s3://ristoh-css-postgres/production/postgresql`.
The 180-day recovery window retains at least six months of daily recovery points.
CloudNativePG's five-minute default `archive_timeout` keeps the expected RPO below
15 minutes. A complete point-in-time restore drill must pass before production launch.

An encrypted Restic repository under `production/cluster-state` protects k3s
snapshots and credentials every six hours with 7 daily, 4 weekly and 6 monthly
snapshots. Preserve the Restic password and age key somewhere other than GitHub,
the cluster, and fil.one. Enable bucket versioning/object retention when fil.one
supports it, and rotate backup credentials independently of application credentials.
The fil.one credentials must permit object deletion within the production prefixes:
Barman retention and `restic forget --prune` cannot enforce retention otherwise.

## Operations

Run `scripts/validate-production.sh` after deployments and node maintenance. Test
failure one server at a time and confirm etcd quorum, PostgreSQL primary continuity,
Argo health and public ingress. Never test two simultaneous node failures: three-node
etcd and PostgreSQL designs tolerate one.

### k3s and PostgreSQL monitoring

The VictoriaMetrics stack scrapes the authenticated Kubernetes API endpoints on
all three servers. In k3s these also expose scheduler/controller-manager metrics;
the component rule groups explicitly use `job="apiserver"`. Do not enable the
default scheduler/controller pod-selector services: k3s has no such pods. Keep
their rule groups enabled so recording rules and availability alerts still run.
Scheduler latency quantiles legitimately have no value when no pods were scheduled
in the five-minute window. `RecordingRulesNoData` excludes only that measured idle
case, not missing scheduler input metrics or other empty recording rules.
`count:up0` uses a boolean comparison plus sum so healthy scrape groups produce
zero rather than no samples; absent inputs still remain absent. vmagent has a
100m CPU request and 500m CPU limit to accommodate scrape bursts without the
throttling observed with the operator's 200m limit; its memory bounds are unchanged.

Embedded etcd needs its own server metrics (the API's etcd *client* metrics are
not a substitute). The k3s role binds TCP 2381 only to loopback and each server's
WireGuard address. `gitops/production/etcd-monitoring.yaml` uses `VMStaticScrape`
to discover the three private addresses explicitly. Argo CD excludes Kubernetes
`Endpoints` and `EndpointSlice` resources, so the chart's legacy static endpoints
cannot be relied on, even when the application reports Synced. The chart's etcd
service/scrape are disabled but its rules and dashboards remain enabled.
No database client certificates, public ingress, or firewall opening
are needed. Keep these addresses aligned with the inventory when replacing nodes.

For an existing cluster, roll out in this order:

1. Review/check the k3s role, then run it with the existing encrypted inventory:
   `ansible-playbook -i ansible/inventory/production.yml ansible/site.yml --tags k3s --ask-vault-pass`.
   This restarts changed servers **one at a time**, waiting for `/readyz` (including
   etcd readiness) before proceeding. Schedule this control-plane maintenance;
   do not run parallel restarts. Verify each private `:2381/metrics` endpoint is
   reachable from vmagent's network and exposes `etcd_server_has_leader 1`.
2. Merge/deploy the monitoring changes through GitOps. Deploying the scrape before
   the listener is available produces real target-down alerts rather than fixing it.
3. Check vmagent `/api/v1/targets`: three etcd targets and three PostgreSQL targets
   must be up. The removed scheduler/controller scrape pools must be absent.
4. Query `scheduler_pending_pods{job="apiserver"}`,
   `etcd_server_has_leader`, and
   `barman_cloud_cloudnative_pg_io_last_available_backup_timestamp{namespace="customer-service-production"}`.
   Check scheduler recording rules produce data during scheduling activity and
   Alertmanager clears the
   missing-target and backup-metrics alerts after rule evaluation.

PostgreSQL uses a native `VMPodScrape` of the CNPG instance exporter on port 9187,
including Barman backup metrics. `enablePodMonitor` is false because the
Prometheus `PodMonitor` CRD is not installed. Keep the backup failure, stale-backup
and missing-metrics alerts enabled. A completed Backup resource is evidence of a
completed job, not a substitute for a restore drill or backup monitoring.

### Grafana traces

Production tracing follows: NGINX web proxy → FastAPI → business operations →
database/outbound calls → OpenTelemetry collector → Tempo →
Grafana's **Tempo** datasource. LangSmith is independent; enabling LangSmith does
not enable or populate Grafana traces. Application tracing is enabled in
`helm/customer-service/values-production.yaml` at a 10% root sampling ratio.
Sampled W3C parent contexts are respected. Health and OAuth callback routes remain
excluded. NGINX exports on the private OTLP/gRPC port configured by
`telemetry.webEndpoint`; Python continues using OTLP/HTTP `telemetry.endpoint`.
Both use `telemetry.enabled` and `telemetry.traceSampleRatio`. The proxy passes
sampling decisions downstream, including an explicit unsampled parent. Changing
the rendered proxy configuration changes its pod checksum and triggers a rollout.
The web image pins the official NGINX **1.30.6 Alpine OTel** image by digest. The
[official image catalog](https://github.com/docker-library/official-images/blob/master/library/nginx)
and [NGINX security advisories](https://nginx.org/en/security_advisories.html) were
checked on 2 November 2026; recheck these and image package advisories on upgrades.

Business spans include `conversation.process`, planning, retrieval, answer
generation, website search and reply persistence; onboarding analysis/submission
and media archival are also traced. Existing issue/provisioning spans remain.
These fixed-name spans do not capture function arguments, results, prompts or
customer messages. Browser JavaScript/RUM and cross-job trace linking are not
included; background jobs may have their own operation-root traces.
Unparented database spans (including routine queue polling) are not sampled.
Database calls inside sampled requests or background operations are retained.
Use existing logs/metrics to investigate standalone database failures; they no
longer create their own traces. This is head sampling, not error-based tail sampling.

The collector strips full URLs/paths/query strings, SQL statements/parameters,
credential-named attributes, and exception messages/stack traces before export.
It also removes client addresses/user-agent attributes and span status messages.
Retained timings, errors/status codes and tenant identifiers are operator data;
keep Grafana access restricted. This filter is not a general PII detector: review
new custom span attributes before sending them.

Tempo uses community chart **3.2.1**, Tempo **3.1.1** (release/security advisory
checks performed 2 November 2026), one replica, a retained 10Gi local-path PVC and
seven-day retention. The release includes Go security updates; the repository's
published critical memcached advisory concerns a component not deployed here.
Recheck advisories on upgrades. No external storage credentials are required.
This is intentionally not an HA or backed-up trace archive: node/disk loss can
lose traces, and retention does not guarantee the disk cannot fill. Alerts cover
Tempo availability and low disk space. The collector has a bounded memory queue
and five-minute retries; prolonged outages or collector restarts can lose queued
spans without blocking customer responses. Previously discarded traces cannot be
recovered.

The service is ClusterIP-only. A NetworkPolicy permits collector OTLP ingestion
and Grafana/vmagent HTTP queries only from selected pods in `monitoring`. Do not
publish Tempo directly: it has no built-in authentication. Existing Kubernetes
administrative access (including port-forward) remains privileged access.

After merge and the normal GitOps deployment, wait for Tempo and the collector
to be ready and the application rollout to complete, then run:

```bash
KUBECONFIG=/private/tmp/production-kubeconfig-16443 bash scripts/check-production-traces.sh
```

This sends a harmless sampled config GET and verifies its exact trace ID contains
connected `web.api` and FastAPI server spans in Tempo. Run it after both web and
application images are upgraded. In Grafana **Explore → Tempo**, look up that ID; ordinary requests are
sampled, so a short quiet time range can legitimately be empty. Initial scope is
trace search/inspection, not service graphs or span-derived metrics dashboards.

Run the disposable local acceptance test (Docker also supported):

```bash
AGENT_TRACE_CONTAINER_TESTS=true AGENT_E2E_CONTAINER_RUNTIME=nerdctl \
  uv run pytest tests/test_trace_pipeline.py -q
```

The test uses the rendered production Tempo and web configurations and real
collector privacy/export processors. It checks an NGINX → FastAPI → operation →
PostgreSQL trace, parent IDs, unsampled context propagation, privacy filtering,
request success during collector outage, and persisted traces after a Tempo restart.
It removes only its uniquely named containers/network/volume and test image.
In Grafana Explore, use `{ span:kind = server }` to find HTTP request traces.
Kubernetes enrichment and
NetworkPolicy enforcement require the post-deployment check. The CI `tracing` job
runs this test with Docker on every PR; local success does not prove CI or live
deployment success.
