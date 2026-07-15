# Customer Support Agent

Increment 4 is a locally runnable vertical slice using FastAPI, LangGraph, LangChain
documents, file-based KB ingestion, conversation routing state, optional LLM-backed answer
generation, and configurable retrieval/answer boundaries. Helm deploys the service with
PostgreSQL and pgvector. No external LLM key is needed for the default local path: a
deterministic extractive generator and local hash embeddings make the workflow inspectable
and reproducible.

## What works

- Customer message ingestion through `POST /messages/customer`.
- Synthetic webhook compatibility through `POST /webhooks/synthetic`.
- Telegram customer text-message webhook through `POST /webhooks/telegram`.
- A LangGraph workflow that persists, retrieves, answers, cites, and flags low-confidence
  answers.
- In-memory adapters for fast development and tests.
- PostgreSQL conversation persistence and pgvector retrieval in Kubernetes.
- Startup knowledge loaded from a mounted directory or ConfigMap.
- Conversation routing state updates for the handoff foundation.
- Optional OpenAI/LangChain answer provider behind `SUPPORT_ANSWER_PROVIDER=openai`.
- Optional OpenAI semantic embeddings behind `SUPPORT_EMBEDDING_PROVIDER=openai`.
- Optional LLM-backed human-request detection behind
  `SUPPORT_HUMAN_REQUEST_DETECTOR_PROVIDER=llm`.
- Helm chart validation and API/graph tests.

Telegram/WhatsApp support group creation, admin KB upload UI, and conversation memory are
intentionally reserved for later increments.

## Run in the IDE

Python 3.10 or newer is required.

```bash
uv sync
uv run uvicorn app.main:app --reload
```

Try the vertical slice:

```bash
curl -X POST http://localhost:8000/messages/customer \
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
`DEBUG` logging. If `OPENAI_API_KEY` is present, it also creates/updates an OpenAI secret
and deploys with `answer.provider=openai`. Override values with environment variables:

```bash
RELEASE_NAME=support LOG_LEVEL=INFO scripts/deploy-local.sh
```

Deploy locally with the OpenAI answer provider:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" scripts/deploy-local.sh
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
  --from-file=./files-dir \
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

## Conversation routing state

The graph tracks one routing state for each conversation:

| Field | Values | Purpose |
|---|---|---|
| `state` | `BOT_ACTIVE`, `HUMAN_REQUESTED`, `HUMAN_ACTIVE` | Who should handle the conversation |

Low-confidence answers return `low_confidence: true` in the response, but they do not change
the persisted conversation state. This keeps the model's uncertainty separate from the
operational handoff workflow. The conversation should move to `HUMAN_REQUESTED` only when
the customer explicitly asks for a human agent, and to `HUMAN_ACTIVE` when a human support
agent accepts or joins the conversation.

The graph detects explicit human-agent requests before answer generation. Local runs use a
deterministic rule-based detector by default; Kubernetes values default to the LLM detector
when an OpenAI API key secret is configured.

`HUMAN_REQUESTED` does not stop the bot from answering. It records that a human has been
requested while the agent continues to use RAG to answer the customer's current and future
questions until a human actually joins and the state becomes `HUMAN_ACTIVE`.

Example response state:

```json
{
  "state": "BOT_ACTIVE",
  "low_confidence": true
}
```

Inspect a conversation:

```bash
curl http://localhost:8000/conversations/<conversation-id>
```

Update state, for example when a human agent accepts a handoff:

```bash
curl -X PATCH http://localhost:8000/conversations/<conversation-id>/state \
  -H 'content-type: application/json' \
  -d '{
    "state": "HUMAN_ACTIVE",
    "reason": "Human support accepted the handoff"
  }'
```

## Conversation history context

For each incoming customer message, the graph loads the exact messages for the current
conversation since `conversation.created_at` and passes them into the answer generator.
The history is still bounded by `SUPPORT_CONVERSATION_HISTORY_MAX_MESSAGES` so very long
open chats do not overfill the LLM context.

The graph also derives compact greeting metadata for the LLM. By default,
`SUPPORT_GREETING_LAPSE_MINUTES=60`, so the prompt tells the LLM to greet the customer on
their first message or when the previous customer message was at least 60 minutes ago.
Absolute timestamps are intentionally omitted from the prompt metadata.

The current prompt context is:

- derived conversation metadata;
- exact current conversation history;
- retrieved KB documents;
- the current customer question.

Raw messages remain the source of truth. Summarized conversation memory is intentionally
reserved for a later increment.

## Optional LLM answer provider

The default answer provider is still deterministic and local:

```env
SUPPORT_ANSWER_PROVIDER=extractive
SUPPORT_HUMAN_REQUEST_DETECTOR_PROVIDER=rules
```

Run the API locally with the OpenAI answer provider:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" scripts/run-local.sh
```

To use an OpenAI-backed LangChain chat model with langsmith tracing, create a Kubernetes secret containing the API
keys:

```bash
kubectl create secret generic api-keys \
  --namespace customer-support \
  --from-literal=OPENAI_API_KEY="key" \
  --from-literal=key="key"
```

Then deploy on kubernetes with:

```bash
helm upgrade --install cs-local helm/customer-support \
  --namespace customer-support \
  --set answer.provider=openai \
  --set llm.existingSecret=openai-api \
  --set llm.model=gpt-4.1-mini
```
or run

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" LANGSMITH_API_KEY="key" scripts/deploy-local.sh
```

The LLM is instructed to answer only from retrieved KB context and return structured JSON
with `answer`, `confidence`, and `grounded`. If no documents are retrieved, or if the
response is not grounded, the graph returns a low-confidence reply.

## Telegram customer webhook

Telegram customer messages are received at:

```text
POST /webhooks/telegram
```

The endpoint currently handles text messages. Non-text updates are acknowledged and ignored.
If `SUPPORT_TELEGRAM_BOT_TOKEN` is configured, the app sends the graph reply back to the
Telegram chat using `sendMessage`.

Create a Secret for the bot token and webhook secret token:

```bash
kubectl create secret generic telegram-bot \
  --namespace customer-support \
  --from-literal=TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  --from-literal=TELEGRAM_WEBHOOK_SECRET_TOKEN="$TELEGRAM_WEBHOOK_SECRET_TOKEN"
```

Deploy with the Secret:

```bash
helm upgrade --install cs-local helm/customer-support \
  --namespace customer-support \
  --set telegram.existingSecret=telegram-bot
```

Register the Telegram webhook after the app has a public HTTPS URL:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -H 'content-type: application/json' \
  -d '{
    "url": "https://example.com/webhooks/telegram",
    "secret_token": "'"$TELEGRAM_WEBHOOK_SECRET_TOKEN"'"
  }'
```





## Configuration

Application configuration uses the `SUPPORT_` prefix. LangSmith uses its native
`LANGSMITH_` names:

| Variable | Default | Purpose |
|---|---|---|
| `SUPPORT_RETRIEVAL_PROVIDER` | `memory` | `memory` or `pgvector` |
| `SUPPORT_ANSWER_PROVIDER` | `extractive` | `extractive` or `openai` |
| `SUPPORT_EMBEDDING_PROVIDER` | `local` | `local` or `openai`; Helm defaults to `openai` for pgvector |
| `SUPPORT_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model when `SUPPORT_EMBEDDING_PROVIDER=openai` |
| `SUPPORT_EMBEDDING_DIMENSIONS` | `64` | pgvector embedding size; Helm defaults to `1536` for OpenAI embeddings |
| `SUPPORT_HUMAN_REQUEST_DETECTOR_PROVIDER` | `rules` | `rules` or `llm`; `llm` uses OpenAI to detect explicit human-agent requests |
| `OPENAI_API_KEY` | unset | Required when `SUPPORT_ANSWER_PROVIDER=openai`, `SUPPORT_EMBEDDING_PROVIDER=openai`, or `SUPPORT_HUMAN_REQUEST_DETECTOR_PROVIDER=llm` |
| `SUPPORT_LLM_MODEL` | `gpt-4.1-mini` | OpenAI chat model used by the LLM answer provider |
| `SUPPORT_LLM_TEMPERATURE` | `0.0` | LLM sampling temperature |
| `SUPPORT_DATABASE_URL` | unset | PostgreSQL connection string |
| `SUPPORT_CONFIDENCE_THRESHOLD` | `0.60` | Below this, mark the response as low confidence |
| `SUPPORT_CONVERSATION_HISTORY_MAX_MESSAGES` | `50` | Safety cap for exact current-conversation messages passed into context |
| `SUPPORT_GREETING_LAPSE_MINUTES` | `60` | Minutes after the previous customer message before the prompt says to greet again |
| `SUPPORT_TELEGRAM_BOT_TOKEN` | unset | Telegram bot token used to send replies with `sendMessage` |
| `SUPPORT_TELEGRAM_WEBHOOK_SECRET_TOKEN` | unset | Optional Telegram webhook secret token checked against `X-Telegram-Bot-Api-Secret-Token` |
| `SUPPORT_SEED_KNOWLEDGE` | `true` | Load startup knowledge |
| `SUPPORT_KNOWLEDGE_PATH` | unset | Directory containing `.md`/`.txt` KB files; no startup documents are loaded when unset |
| `SUPPORT_KNOWLEDGE_CHUNK_SIZE` | `1200` | Character target size for seed KB chunks before embedding |
| `SUPPORT_KNOWLEDGE_CHUNK_OVERLAP` | `200` | Character overlap between adjacent seed KB chunks |
| `SUPPORT_LOG_LEVEL` | `INFO` | Application log level, for example `DEBUG` |
| `SUPPORT_LOG_FORMAT` | `{asctime} - {levelname}:{name}:{message}` | Python logging format using `{}` style |
| `LANGSMITH_TRACING` | `true` | Enable LangSmith tracing |
| `LANGSMITH_TRACING_V2` | `true` | Enable LangSmith tracing v2 |
| `LANGSMITH_ENDPOINT` | `https://eu.api.smith.langchain.com` | LangSmith endpoint |
| `LANGSMITH_PROJECT` | `customer-support` | LangSmith project name |

Changing `SUPPORT_EMBEDDING_DIMENSIONS` changes the required pgvector column type. Use a
fresh database, recreate the `knowledge_documents` table, or reindex the KB when moving
between local 64-dimensional embeddings and OpenAI 1536-dimensional embeddings.
Seed KB files are chunked before embedding, so pgvector stores one row per chunk using a
stable `chunk_id` such as `kb/menu.txt#0000`.
API response citations return these chunk ids, not just source file paths, so callers can
trace an answer to the exact retrieved chunk.

## Increment 4 boundary

A `low_confidence: true` response indicates low answer confidence but does not persist a
handoff state. The graph can move a conversation to `HUMAN_REQUESTED` when the customer
explicitly asks for a human agent, but creating/reusing Telegram or WhatsApp support
groups, tracking internal agent discussion, deciding which agent messages to forward, and
forwarding those messages to the customer still belong to later increments.
