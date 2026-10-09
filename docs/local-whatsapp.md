# Local WhatsApp signup and credential storage

`scripts/deploy-local.sh` bootstraps a separate persistent OpenBao when WhatsApp
signup is enabled (the default). This is **Rancher Desktop Kubernetes only**;
the script pins every Kubernetes/Helm operation to `rancher-desktop` and does not
change the current kube context. Do not point it at production.

## First setup

Start Rancher Desktop with Kubernetes/containerd enabled. Install `kubectl`,
`helm`, `nerdctl`, `uv` and `openssl`. Keep the existing local PostgreSQL, API-key
and application setup used by `deploy-local.sh`.

Create an ignored `local-docs/local-meta.env` file containing your **development**
Meta configuration (not production vault credentials):

```dotenv
AGENT_META_APP_ID=your-development-app-id
AGENT_META_SIGNUP_CONFIGURATION_ID=your-signup-configuration-id
AGENT_META_APP_SECRET=your-development-app-secret
AGENT_META_WEBHOOK_VERIFICATION_TOKEN=your-random-local-verification-token
```

Protect the file and create a dedicated local Secret, keeping it separate from
any existing `app-configs` keys:

```bash
chmod 600 local-docs/local-meta.env
kubectl --context rancher-desktop create namespace customer-service \
  --dry-run=client -o yaml | kubectl --context rancher-desktop apply -f -
kubectl --context rancher-desktop -n customer-service create secret generic local-meta-configs \
  --from-env-file=local-docs/local-meta.env --dry-run=client -o yaml \
  | kubectl --context rancher-desktop apply -f -

LOCAL_META_CONFIG_SECRET=local-meta-configs \
WEB_PUBLIC_BASE_URL=https://your-tunnel.ngrok-free.app \
  bash scripts/deploy-local.sh
```

Alternatively put all four keys in the local `app-configs` Secret and omit
`LOCAL_META_CONFIG_SECRET`. The bootstrap checks non-empty key names without
printing values. It does not decrypt SOPS, copy production secrets, or create a
Meta app. Real Meta permissions, allowed domains and HTTPS webhook/tunnel setup
must still be configured. Point the tunnel to the local web service; the webhook
path is `/api/webhooks/whatsapp`. The same public URL must be used by the browser.

## What happens

- The pinned official OpenBao chart runs one replica in `openbao-local`, with
  a 1-GiB local-path PVC, private ClusterIP services and verified TLS.
- Bootstrap generates a **new local** static seal key and self-signed TLS
  certificate. No production peers, fil.one credentials, backups, cert-manager
  or monitoring CRDs are used. The development vault has no NetworkPolicy;
  authentication/policies protect access within this trusted local cluster.
- It initializes once and configures KV v2 plus Kubernetes authentication. A
  role bound to the exact local app namespace and service account grants only
  the WhatsApp credential prefix. The application receives a projected JWT and
  the public CA, never the root token or seal key.
- Completing signup writes the access token and registration PIN directly to
  `tenant-credentials/whatsapp/<connection-id>` in **local OpenBao**. PostgreSQL
  stores only a reference. No tenant Kubernetes Secret is created.
- Repeat deploys reuse the key, certificate, initialization and persistent data.
  Restarts auto-unseal. Missing/mismatched recovery files stop bootstrap instead
  of silently replacing keys or deleting data.

Local recovery files are owner-only under
`local-docs/openbao-local/rancher-desktop/` (ignored by Git). Unlike production,
the development bootstrap root token is retained there for repeat configuration;
never share that directory or mount it into application pods. Protect it along
with the local database if you need to preserve development connections. TLS
certificates expire after one year; bootstrap refuses expired certificates.
This local setup is not a production security or backup configuration.

You can bootstrap without rebuilding app images:

```bash
uv run python scripts/bootstrap-local-openbao.py \
  --context rancher-desktop --namespace customer-service \
  --service-account cs-local-customer-service-app \
  --role customer-service-cs-local --meta-secret local-meta-configs
```

For development without WhatsApp:

```bash
AGENT_ONBOARDING_WHATSAPP_ENABLED=false bash scripts/deploy-local.sh
```

This skips vault bootstrap and explicitly disables the feature; it does **not**
delete an existing vault or credentials. Add
`AGENT_ONBOARDING_TELEGRAM_ENABLED=true` if you want the legacy setup screens.

Browser QA still uses mocked Meta/OpenBao endpoints. Do not use its dummy
configuration or `AGENT_DEPLOYMENT_ENVIRONMENT=test` for real customer signup.

## Resuming in another browser

The session ID in a resume link identifies a draft; it does not authorize WhatsApp
or Drive access. If the `onboarding-<session-id>` cookie is missing or expired,
the WhatsApp screen offers **Verify this browser**. Request a code, enter it in
the same browser, then continue from the saved step. The email goes only to the
session's existing account address. Account verification and the draft are not reset.

Recovery uses separate HMAC-hashed codes, a Strict/HttpOnly challenge cookie,
a ten-minute expiry, five attempts, and a 60-second resend cooldown. Resending
invalidates the earlier challenge. Successful recovery replaces the session's
authorized-browser token, so the previously authorized browser must reverify
before further protected operations. Cookies are Secure when the public URL is HTTPS.
