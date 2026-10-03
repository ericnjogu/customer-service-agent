# WhatsApp credentials: staged OpenBao rollout

This directory is deliberately outside the watched production GitOps tree.
Merging the application code does **not** deploy OpenBao. WhatsApp signup is
enabled by default (`onboarding.whatsappEnabled=true`), but `openbao.enabled`
remains false until explicitly configured. Deployments awaiting bootstrap must
override `onboarding.whatsappEnabled=false`; otherwise missing vault configuration
fails application startup. Keep that override until acceptance;
`onboarding.telegramEnabled` also defaults false. Existing Telegram messaging is
independent of the setup flag.

## Prerequisites and deployment order

1. Confirm three schedulable nodes, local-path storage, cert-manager, functioning
   network-policy enforcement, and capacity for three 256-MiB requests / 768-MiB
   limits plus snapshot jobs. Review rendered manifests before applying.
2. Bootstrap the existing SOPS/age mechanism independently of OpenBao. Prepare
   `openbao-seal` using exactly 32 cryptographically random bytes. Encrypt its
   Kubernetes manifest with SOPS; never commit the plaintext example values or
   put the age private key in this repository. Use `infra/openbao/secrets.yaml`
   only as an ignored local working file. Add an explicit SOPS creation rule for
   the chosen encrypted path before encryption. Apply into namespace `openbao`
   before starting the servers; do not place the unseal key in app-configs.
3. Reuse the existing fil.one `backup-credentials` key, as selected by the operator.
   Prepare its OpenBao-namespace copy without printing or writing plaintext:

   ```bash
   SOPS_AGE_KEY_FILE="$PWD/local-docs/production-age-key" \
     uv run python scripts/prepare-openbao-backup-secret.py
   ```

   This validates that the four Meta entries are populated (not their validity),
   leaves the production source file unchanged, and creates the SOPS-encrypted
   `infra/openbao/backup-credentials.enc.yaml`. After creating namespace `openbao`,
   apply that **encrypted SopsSecret** through the existing SOPS operator. It
   creates `openbao-backup-s3` only in that namespace; the application does not
   mount it. The file remains outside the automatic production GitOps tree.

   The shared key needs read/write/list/delete access to `ristoh-css-postgres`.
   `production/openbao/` separates object names, not permissions. A compromise
   or revocation affects both PostgreSQL and OpenBao backups. When rotating,
   update the original production secret, regenerate this encrypted copy, apply
   both, validate both backup systems, and only then revoke the old key.
4. Review `helm/openbao-platform` and its locked official dependency. Apply
   `application.yaml` only when ready, and explicitly sync it in Argo CD. There
   is no automatic sync policy. No Ingress or public load balancer is created.
5. Through the existing Kubernetes tunnel, port-forward pod `openbao-0` to
   localhost:8200. Export `BAO_ADDR=https://localhost:8200` and `BAO_CACERT` pointing
   to a locally saved copy of the certificate's **public** CA certificate.
   Never disable certificate verification.
6. Check `bao status`. Initialize **once**, only if uninitialized, using
   `bao operator init` with separately held PGP recipients for recovery shares
   and the initial root token. Store the encrypted output in independent recovery
   storage, not CI artifacts, terminal transcripts, GitHub, or the backup bucket.
   Wait for all three replicas to join; inspect `bao operator raft list-peers`.

   For this deployment, the operator selected owner-only recovery files in
   `local-docs/openbao-recovery`. `scripts/initialize-openbao.py` captures the
   initialization response there before interpreting it. These files are not
   encrypted at rest by SOPS: independently protect and copy them to recovery
   storage before considering disaster recovery complete. Never rerun
   initialization against an existing vault or delete volumes to recover keys.

## Initial authorization (operator procedure)

Run the following with a temporary authorized bootstrap session. Commands show
configuration, not credentials. Re-running mount/auth enable commands on an
existing installation is unnecessary: inspect first, never disable/recreate them.

```bash
bao secrets enable -path=tenant-credentials kv-v2
bao auth enable kubernetes
bao write auth/kubernetes/config kubernetes_host=https://kubernetes.default.svc
bao policy write customer-service infra/openbao/application-policy.hcl
bao policy write snapshot infra/openbao/snapshot-policy.hcl
bao write auth/kubernetes/role/customer-service \
  bound_service_account_names=one-css-customer-service-app \
  bound_service_account_namespaces=customer-service-production \
  audience=openbao token_policies=customer-service token_ttl=10m token_max_ttl=30m
bao write auth/kubernetes/role/snapshot \
  bound_service_account_names=openbao-backup \
  bound_service_account_namespaces=openbao \
  token_policies=snapshot token_ttl=10m token_max_ttl=15m
```

Audit logging is configured declaratively in the server HCL (`audit "file"
"to-stdout"`). OpenBao 2.7 rejects unsafe API audit creation by default; do not
enable the unsafe override. `scripts/configure-openbao.py` idempotently applies
the policies/roles, verifies that audit device, and tests a restricted
`openbao-operator` service-account login. The operator policy grants health,
Raft inspection and snapshots, not credential reads or administrative changes.

Use the server's rotating local Kubernetes reviewer token, not a copied token.
The official chart supplies the server TokenReview binding. Verify the rendered
application service-account name before writing its role. The application uses
a dedicated projected JWT with audience `openbao`; snapshot jobs use their own
service account. The client reauthenticates before expiry rather than holding a
permanent vault token. No application policy grants vault administration.

Establish and test a limited operator authentication method and break-glass
procedure, then revoke the initial root token (`bao token revoke -self`). Do not
retain a root token in a Kubernetes Secret. Keep recovery shares, all seal keys
needed by retained snapshots, and the SOPS age private key in separate protected
recovery storage outside this cluster, GitHub, and fil.one.

Copy only `ca.crt` into the application's `openbao-ca` ConfigMap in
`customer-service-production`. Never copy `tls.key` or the seal key. Set
`openbao.enabled=true` to mount the projected JWT, public CA, and Meta app-configs
references, leaving `onboarding.whatsappEnabled=false` while testing. The four
Meta settings are listed in the production secrets example; encrypt actual
values in the existing app-configs SOPS manifest. Browser configuration exposes
only app ID, signup configuration ID, API version and feature flags.

## Backup and recovery acceptance

`backups.endpoint` matches the existing PostgreSQL fil.one endpoint. Enable backups
only after testing the shared credentials and snapshot role. Four independent CronJobs use the
official snapshot agent: every 15 minutes (one day), daily (seven days), weekly
(28 days), monthly (186 days). Retention is **age-based**, not exact object count;
month length and retries can retain extra recovery points. Each tier has its own
prefix. The agent deletes only objects older than that tier's window after a
successful snapshot upload. Check provider bucket versioning/lifecycle separately:
deleted versions may continue consuming storage.

Before signup activation, manually run a snapshot job, confirm a non-empty
uploaded object, and restore it into an **isolated** deployment with outbound Meta
traffic blocked. Never point the drill at production PVCs or the production
Kubernetes context. Use the same server version and original static seal key:

1. Download a selected snapshot using an independent recovery credential.
2. Build an isolated three-node deployment with separate PVCs, trusted TLS and
   the original seal configuration. Restore using `bao operator raft snapshot
   restore` following the version's recovery procedure; a force restore is only
   appropriate for the isolated replacement and must be explicitly reviewed.
3. Recover operator access, reconfigure Kubernetes authentication for the target
   cluster, and bind the target application service account. Issue new short-lived
   client credentials; do not reuse old service-account JWTs.
4. Read a synthetic connection credential via the application adapter, without
   printing its value. Reconcile PostgreSQL `credential_reference` UUIDs with
   restored vault paths. Database entries newer than the snapshot must remain
   disabled until reconnected; never silently fall back to Kubernetes Secrets.
5. Record snapshot age, recovery duration, missing references, and policy tests.
   Destroy only the explicitly identified drill deployment after review.

Losing one node must preserve two-peer quorum. Restart one pod at a time and
verify unattended auto-unseal; never initialize an already initialized cluster.
Loss of quorum, storage, or the seal key requires recovery, not merely a restart.
Static auto-unseal does not protect against a sufficiently privileged Kubernetes
administrator who can obtain both storage and the mounted seal key.

## Operations and remaining rollout gates

Require working alerts for backup freshness, quorum/readiness, PVC pressure,
certificate expiry and authentication failures before enabling signup. Audit
readiness/capacity/backup rules are included in the chart using kube-state-metrics;
verify their series and notification routing in the running monitoring stack.
`gitops/production/openbao-audit-alerts.yaml` supplies a Loki rule for five or more
failed Kubernetes logins in five minutes, sustained for two minutes. The existing
single-binary Loki mounts that rule and sends it to Alertmanager. Audit
logging uses default HMAC redaction; do not enable raw audit logging. Verify the
existing log collector includes the `openbao` namespace without exposing values.
Certificate rotation requires validating that all server processes use the new
certificate; use a one-at-a-time restart if necessary, checking quorum each time.

Meta app readiness, required advanced permissions, business verification,
allowed domain/redirect configuration and billing are external prerequisites.
Configure the app webhook at `https://css.ristoh.co.ke/api/webhooks/whatsapp`;
verify signed events with a test business before enabling real customer signup.
Set `onboarding.whatsappEnabled=true` only after recovery and real acceptance pass.

### Email alerts and staged application release

Operational warning/critical alerts route to `alerts@ristoh.co.ke`. Run
`SOPS_AGE_KEY_FILE="$PWD/local-docs/production-age-key" uv run python
scripts/prepare-alert-email-secret.py` to regenerate the encrypted Alertmanager
configuration after changing the existing Resend key or `AGENT_EMAIL_FROM`.
Only ciphertext is written, in `gitops/production/secrets/alert-email.enc.yaml`.
SMTP uses Resend on port 587 with mandatory TLS. The monitoring credential is a
copy: rotation requires regenerating and reconciling it as well as app secrets.
Verify a synthetic alert reaches the mailbox after GitOps reconciliation; a
healthy SopsSecret alone is not proof of email delivery.

The production override deliberately keeps `onboarding.whatsappEnabled=false`
while enabling the vault configuration. The application default remains true.
Merge the deployment PR, wait for browser QA/image publication and Argo health,
then verify webhook challenge/signature handling before enabling a controlled
Meta acceptance session. Existing Telegram messaging remains available. Do not
advertise customer signup until that session and operational gates pass.

The operator reported copying recovery material to Google Drive on 2026-10-03.
Keep that folder private, protect the account with MFA, and verify retrieval;
this report does not verify its sharing settings or restoration accessibility.

The signup browser is authorized by the account-verification cookie for 24 hours;
attempts expire after ten minutes and can be consumed once. A connected number
cannot receive automated replies until its provisioning job succeeds. Webhook
deduplication is durable: a send timeout remains an unknown delivery, not proof
of failure, and is not automatically resent (Meta lacks our idempotency key).
Provisioning is serialized by a PostgreSQL advisory lock. After a process crash,
the verified browser can use **Retry interrupted provisioning**; a live worker
holding the lock rejects the retry. No separate provisioning recovery daemon is
introduced. A replay reuses stored progress and stable credential paths.

## Local validation

```bash
AGENT_E2E_CHANNEL=whatsapp bash scripts/run-browser-qa.sh
AGENT_E2E_CHANNEL=telegram bash scripts/run-browser-qa.sh
helm dependency build helm/openbao-platform
helm lint helm/openbao-platform
AGENT_OPENBAO_RAFT_TESTS=true uv run pytest tests/test_openbao_raft.py -q
```

Browser QA starts/stops disposable PostgreSQL, Redis and WireMock; the browser
talks to the real application, with Meta SDK and external HTTP services mocked.
CI also runs real KV/policy tests against disposable OpenBao and a single-node
Raft restart/auto-unseal/snapshot-restore test using freshly generated temporary
seal material. These do **not** certify production quorum, TLS/network policies,
fil.one access, retention, or a full off-cluster recovery drill.

### Production bootstrap validation — 2026-10-03

- Three voting replicas passed sequential restart and unattended auto-unseal.
- Application service-account authentication passed over private TLS; a synthetic
  credential was written/read and administrative policy access was denied.
- A real snapshot uploaded to `ristoh-css-postgres/production/openbao/frequent/`.
- `scripts/verify-openbao-backup.py` downloaded that snapshot and restored it into
  a disposable local vault using the original seal key. The synthetic credential
  matched; recovery took 17 seconds. The container and downloaded snapshot were
  removed. No production vault data was restored or replaced.
- This was a single-node data-recovery check, **not** a replacement three-node
  cluster authentication/recovery drill. Alert delivery validation and real Meta
  acceptance remain launch gates. Application rollout and customer signup are
  not yet complete.
- After adding a bounded TLS-readiness init container, scheduled 15-minute
  snapshots succeeded over several hours. Earlier connection-refused failures
  showed that a one-off successful backup was not enough to certify scheduling.
- Loki aggregate queries confirmed receipt of redacted audit login events.

The bootstrap check script relies on the initial token retained in the snapshot;
it is not a general recovery procedure for snapshots taken after root revocation.
Future recovery must use the separately protected recovery shares to establish
temporary operator access. `scripts/revoke-openbao-bootstrap.py` revokes the
temporary bootstrap token and records confirmation without printing it.

References: [Kubernetes authentication](https://openbao.org/docs/auth/kubernetes/),
[static seal](https://openbao.org/docs/configuration/seal/static/),
[snapshot agent v0.4.5](https://github.com/openbao/openbao-snapshot-agent/tree/v0.4.5).
