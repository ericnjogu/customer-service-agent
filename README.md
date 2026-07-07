# Customer Support Agent

Increment 2 is a locally runnable vertical slice using FastAPI, LangGraph, LangChain
documents, file-based KB ingestion, and a configurable retrieval boundary. Helm deploys
the service with PostgreSQL and pgvector. No external LLM key is needed yet: a deterministic
extractive generator and local hash embeddings make the workflow inspectable and
reproducible.

## What works

- Synthetic webhook ingestion through `POST /webhooks/synthetic`.
- A LangGraph workflow that persists, retrieves, answers, cites, and marks low-confidence
  answers for future escalation.
- In-memory adapters for fast development and tests.
- PostgreSQL conversation persistence and pgvector retrieval in Kubernetes.
- Startup knowledge loaded from a mounted directory, with demo knowledge as the local fallback.
- Helm chart validation and API/graph tests.

Human handover, Telegram, admin KB upload UI, conversation memory, and production LLM/embedding providers are
intentionally reserved for later increments.

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
  --set knowledge.existingConfigMap=support-knowledge
```

The app reads `.md` and `.txt` files from `/knowledge` by default. ConfigMaps are suitable
for these small 4 KB documents, but keep the total ConfigMap size comfortably below
Kubernetes' 1 MiB object limit.

## Configuration

Environment variables use the `SUPPORT_` prefix:

| Variable | Default | Purpose |
|---|---|---|
| `SUPPORT_RETRIEVAL_PROVIDER` | `memory` | `memory` or `pgvector` |
| `SUPPORT_DATABASE_URL` | unset | PostgreSQL connection string |
| `SUPPORT_CONFIDENCE_THRESHOLD` | `0.60` | Below this, mark for escalation |
| `SUPPORT_SEED_KNOWLEDGE` | `true` | Load startup knowledge |
| `SUPPORT_KNOWLEDGE_PATH` | unset | Directory containing `.md`/`.txt` KB files; demo knowledge is used when unset |

## Increment 2 boundary

An `escalated: true` response is still only a decision signal. Creating or reusing a human
support group belongs to Increment 3. This increment adds an operator-driven KB ingestion
path, not yet an admin upload UI or document approval workflow.
