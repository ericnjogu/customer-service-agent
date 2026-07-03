# Customer Support Agent

Increment 1 is a locally runnable vertical slice using FastAPI, LangGraph, LangChain
documents, and a configurable retrieval boundary. Helm deploys the service with PostgreSQL
and pgvector. No external LLM key is needed yet: a deterministic extractive generator and
local hash embeddings make the workflow inspectable and reproducible.

## What works

- Synthetic webhook ingestion through `POST /webhooks/synthetic`.
- A LangGraph workflow that persists, retrieves, answers, cites, and marks low-confidence
  answers for future escalation.
- In-memory adapters for fast development and tests.
- PostgreSQL conversation persistence and pgvector retrieval in Kubernetes.
- Seed knowledge for password-reset and refund questions.
- Helm chart validation and API/graph tests.

Human handover, Telegram, conversation memory, and production LLM/embedding providers are
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
helm upgrade --install cs-iteration-0x helm/customer-support
kubectl rollout status statefulset/support-customer-support-postgres
kubectl rollout status deployment/support-customer-support
kubectl port-forward service/support-customer-support 8000:8000
```

In another terminal, send the same synthetic webhook shown above. Inspect the deployment
with:

```bash
kubectl get pods
kubectl logs deployment/support-customer-support
```

The `Dockerfile` remains in the repository because it is the standard OCI image build
recipe; `nerdctl`, rather than Docker, builds it for Kubernetes.

The default password in `values.yaml` is deliberately local-only. Override it outside local
development and use a secret manager in production.

## Configuration

Environment variables use the `SUPPORT_` prefix:

| Variable | Default | Purpose |
|---|---|---|
| `SUPPORT_RETRIEVAL_PROVIDER` | `memory` | `memory` or `pgvector` |
| `SUPPORT_DATABASE_URL` | unset | PostgreSQL connection string |
| `SUPPORT_CONFIDENCE_THRESHOLD` | `0.60` | Below this, mark for escalation |
| `SUPPORT_SEED_KNOWLEDGE` | `true` | Load demo knowledge at startup |

## Increment 1 boundary

An `escalated: true` response is only a decision signal in this increment. Creating or
reusing a human support group belongs to Increment 3. This keeps the first review point
small while proving the graph, persistence, retrieval abstraction, container, and chart.
