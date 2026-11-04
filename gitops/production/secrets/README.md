# Production secrets

Commit only SOPS-encrypted `SopsSecret` resources in this directory. The production
age private key is never stored in Git. Bootstrap it as follows after installing the
operator namespace:

```bash
kubectl create namespace sops --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sops create secret generic sops-age-key-file \
  --from-file=key="$SOPS_AGE_KEY_FILE"
```

Copy `production-secrets.example.yaml` to an untracked temporary path, populate it,
and encrypt the `secretTemplates` structure:

```bash
sops encrypt --age "$SOPS_AGE_RECIPIENTS" \
  --encrypted-suffix Templates \
  /private/tmp/production-secrets.yaml \
  > gitops/production/secrets/production-secrets.enc.yaml
```

The encrypted output is safe to commit. Confirm it contains no plaintext values.

## Grafana administrator

`grafana-admin.enc.yaml` creates the `monitoring/grafana-admin` Secret using the
existing production SOPS recipient. The VictoriaMetrics chart references it via
`grafana.admin.existingSecret`; do not revert to a chart-generated password.
Argo CD renders charts without access to the live Secret lookup, so random
password generation can change the bootstrap Secret without updating Grafana's
persistent account database.

Read `admin-user` and `admin-password` from `grafana-admin`, not the retired
chart-generated `vm-grafana` Secret. The Grafana service and deployment are still
named `vm-grafana`. During rollout, apply the SopsSecret and confirm its child
Secret is ready before syncing the chart change. The earlier sync wave controls
submission order, but is not a guarantee that decryption has finished.

For future password rotation, edit the encrypted resource with SOPS, reconcile
the generated Secret, and reset the existing Grafana administrator using the CLI
with `--password-from-stdin`. Changing the Secret or restarting Grafana alone
does **not** rotate an existing database password. Keep shell tracing disabled;
never put passwords in command arguments, logs, or plaintext Git files. Do not
delete the database or PVC. The initial migration preserves the current Secret
credentials and synchronizes the existing administrator password once.

`app-configs` must include a random value of at least 32 characters under
`AGENT_ONBOARDING_VERIFICATION_CODE_SECRET`. The application uses it only as the
HMAC-SHA256 key for onboarding email codes; rotate it only when outstanding
ten-minute codes may be invalidated.

The `backup-credentials` template uses fil.one S3-compatible credentials scoped to
the private `ristoh-css-postgres` bucket. PostgreSQL and OpenBao backups share this
key by operator choice. OpenBao has a separate encrypted copy for its namespace;
run `scripts/prepare-openbao-backup-secret.py` after any rotation and apply both
SopsSecrets before revoking the old key. Prefixes do not isolate access: either
copy can access both backup sets. See `infra/openbao/README.md` for the procedure.
