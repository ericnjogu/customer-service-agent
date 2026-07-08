# Customer Support Agent

Increment 3 is a locally runnable vertical slice using FastAPI, LangGraph, LangChain
documents, file-based KB ingestion, issue status tracking, and a configurable retrieval
boundary. Helm deploys the service with PostgreSQL and pgvector. No external LLM key is
needed yet: a deterministic extractive generator and local hash embeddings make the
workflow inspectable and reproducible.

## What works

- Synthetic webhook ingestion through `POST /webhooks/synthetic`.
- A LangGraph workflow that persists, retrieves, answers, cites, and marks low-confidence
  answers for future escalation.
- In-memory adapters for fast development and tests.
- PostgreSQL conversation persistence and pgvector retrieval in Kubernetes.
- Startup knowledge loaded from a mounted directory, with demo knowledge as the local fallback.
- Conversation handling status and issue status updates for the handoff foundation.
- Helm chart validation and API/graph tests.

Actual support group creation, Telegram, admin KB upload UI, conversation memory, and
production LLM/embedding providers are intentionally reserved for later increments.

## Run in the IDE

Python 3.10 or newer is required.

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Try the vertical slice:

```bash
curl -X POST http://localhost:8000/webhooks/synthetic \
  -H 'content-type: application/json' \
  -d '{
    "event_id": "demo-1",
    "external_chat_id": "chat-1",
    "external_user_id": "user-1",
    "text": "How do I reset my password?"
  }'
```

Run checks:

```bash
uv run pytest
uv run ruff check .
helm lint helm/customer-support
```

## Run with Rancher Desktop Kubernetes

The local deployment targets Rancher Desktop's Kubernetes cluster. The image is built
directly into Rancher Desktop's `k8s.io` containerd namespace, so no registry or Docker
daemon is required.

```bash
kubectl config use-context rancher-desktop
nerdctl --namespace k8s.io build -t customer-support:local .
kubectl create namespace customer-support
helm upgrade --install support helm/customer-support --namespace customer-support
kubectl rollout status statefulset/support-customer-support-postgres --namespace customer-support
kubectl rollout status deployment/support-customer-support-app --namespace customer-support
kubectl port-forward service/support-customer-support-app 8000:8000 --namespace customer-support
```

In another terminal, send the same synthetic webhook shown above. Inspect the deployment
with:

```bash
kubectl get pods --namespace customer-support
kubectl logs deployment/support-customer-support-app --namespace customer-support
```

### Redeploy after changing Python code

Helm only recreates Pods when the rendered Kubernetes Pod template changes. Rebuilding
`customer-support:local` changes the local image contents, but the Deployment still points
to the same image tag, so Kubernetes will keep running the existing Pods until you restart
the rollout or use a new image tag.

For local development with the `cs-local` release:

```bash
nerdctl --namespace k8s.io build -t customer-support:local .

helm upgrade --install cs-local helm/customer-support \
  --namespace customer-support \
  --set logging.level=DEBUG

kubectl rollout restart deployment/cs-local-customer-support-app \
  --namespace customer-support

kubectl rollout status deployment/cs-local-customer-support-app \
  --namespace customer-support
```

Then check the new Pod logs:

```bash
kubectl logs deployment/cs-local-customer-support-app \
  --namespace customer-support
```

Or run the helper script:

```bash
scripts/deploy-local.sh
```

The script defaults to `cs-local`, `customer-support`, `customer-support:local`, and
`DEBUG` logging. Override those values with environment variables:

```bash
RELEASE_NAME=support LOG_LEVEL=INFO scripts/deploy-local.sh
```

The `Dockerfile` remains in the repository because it is the standard OCI image build
recipe; `nerdctl`, rather than Docker, builds it for Kubernetes.

The default password in `values.yaml` is deliberately local-only. Override it outside local
development and use a secret manager in production.

### Mount local KB files through a ConfigMap

Keep the actual KB files outside the repository, for example:

```bash
mkdir -p "$HOME/customer-support-knowledge"
cat > "$HOME/customer-support-knowledge/refunds.md" <<'EOF'
Refund requests can be submitted within 30 days of purchase. Include the order number
and the reason for the request.
EOF
```

Create or update a ConfigMap from that directory:

```bash
kubectl create configmap seed-knowledge \
  --namespace customer-support \
  --from-file=drinks.txt \
  --from-file=menu-gpt-4.txt \
  --dry-run=client \
  -o yaml | kubectl apply -f -
```

or using selected files:

```bash
kubectl create configmap seed-knowledge \
  --namespace customer-support \
  --from-file=drinks.txt \
  --from-file=menu-gpt-4.txt \
  --dry-run=client \
  -o yaml | kubectl apply -f -

```

Then mount it into the app:

```bash
helm upgrade --install support helm/customer-support \
  --namespace customer-support \
  --set knowledge.existingConfigMap=seed-knowledge
```

The app reads `.md` and `.txt` files from `/knowledge` by default. ConfigMaps are suitable
for these small 4 KB documents, but keep the total ConfigMap size comfortably below
Kubernetes' 1 MiB object limit.

## Conversation and issue status

The graph now tracks two related status concepts:

| Field | Values | Purpose |
|---|---|---|
| `status` | `BOT_ACTIVE`, `HANDOFF_PENDING`, `HUMAN_ACTIVE` | Who should handle the conversation |
| `issue_status` | `NEW`, `IN_PROGRESS`, `CLOSED`, `ESCALATED`, `REOPENED` | Support issue lifecycle |

When the graph produces a low-confidence answer, it marks the conversation as:

```json
{
  "status": "HANDOFF_PENDING",
  "issue_status": "ESCALATED"
}
```

Inspect a conversation:

```bash
curl http://localhost:8000/conversations/<conversation-id>
```

Update status, for example when a human agent accepts a handoff:

```bash
curl -X PATCH http://localhost:8000/conversations/<conversation-id>/status \
  -H 'content-type: application/json' \
  -d '{
    "status": "HUMAN_ACTIVE",
    "issue_status": "IN_PROGRESS",
    "reason": "Human support accepted the handoff"
  }'
```

## Configuration

Environment variables use the `SUPPORT_` prefix:

| Variable | Default | Purpose |
|---|---|---|
| `SUPPORT_RETRIEVAL_PROVIDER` | `memory` | `memory` or `pgvector` |
| `SUPPORT_DATABASE_URL` | unset | PostgreSQL connection string |
| `SUPPORT_CONFIDENCE_THRESHOLD` | `0.60` | Below this, mark for escalation |
| `SUPPORT_SEED_KNOWLEDGE` | `true` | Load startup knowledge |
| `SUPPORT_KNOWLEDGE_PATH` | unset | Directory containing `.md`/`.txt` KB files; demo knowledge is used when unset |
| `SUPPORT_LOG_LEVEL` | `INFO` | Application log level, for example `DEBUG` |
| `SUPPORT_LOG_FORMAT` | `{asctime} - {levelname}:{name}:{message}` | Python logging format using `{}` style |

## Increment 3 boundary

An `escalated: true` response now persists a handoff-pending status. Creating or reusing
a real Telegram/WhatsApp support group, forwarding selected agent messages, and exposing
an admin console still belong to later increments.
