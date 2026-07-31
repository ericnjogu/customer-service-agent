# Customer Service Agent

Increment 4 is a locally runnable vertical slice using FastAPI, LangGraph, LangChain
documents, tenant-scoped KB retrieval, conversation routing state, optional LLM-backed answer
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
- Tenant-scoped KB retrieval through the configured retrieval store.
- Conversation routing state updates for the handoff foundation.
- Optional OpenAI/LangChain answer provider behind `AGENT_ANSWER_PROVIDER=openai`.
- Optional OpenAI semantic embeddings behind `AGENT_EMBEDDING_PROVIDER=openai`.
- Optional question planner behind `AGENT_QUESTION_PLANNER_PROVIDER=llm` that routes
  out-of-scope questions and decides whether conversation history is needed.
- Optional LLM-backed human-request detection behind
  `AGENT_HUMAN_REQUEST_DETECTOR_PROVIDER=llm`.
- Helm chart validation and API/graph tests.

Telegram/WhatsApp support group creation, admin KB/source management UI, live source tools,
cloud-document connectors, multimedia handling, and conversation memory are intentionally
reserved for later increments.

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
    "tenant_id": "default",
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
helm lint helm/customer-service
```

## Run with Rancher Desktop Kubernetes

The local deployment targets Rancher Desktop's Kubernetes cluster. The image is built
directly into Rancher Desktop's `k8s.io` containerd namespace, so no registry or Docker
daemon is required.

```bash
kubectl config use-context rancher-desktop
nerdctl --namespace k8s.io build -t customer-service:local .
kubectl create namespace customer-service
helm upgrade --install support helm/customer-service --namespace customer-service
kubectl rollout status statefulset/support-customer-service-postgres --namespace customer-service
kubectl rollout status deployment/support-customer-service-app --namespace customer-service
kubectl port-forward service/support-customer-service-app 8000:8000 --namespace customer-service
```

In another terminal, send the same synthetic webhook shown above. Inspect the deployment
with:

```bash
kubectl get pods --namespace customer-service
kubectl logs deployment/support-customer-service-app --namespace customer-service
```

### Redeploy after changing Python code

Helm only recreates Pods when the rendered Kubernetes Pod template changes. Rebuilding
`customer-service:local` changes the local image contents, but the Deployment still points
to the same image tag, so Kubernetes will keep running the existing Pods until you restart
the rollout or use a new image tag.

For local development with the `cs-local` release:

```bash
nerdctl --namespace k8s.io build -t customer-service:local .

helm upgrade --install cs-local helm/customer-service \
  --namespace customer-service \
  --set logging.level=DEBUG

kubectl rollout restart deployment/cs-local-customer-service-app \
  --namespace customer-service

kubectl rollout status deployment/cs-local-customer-service-app \
  --namespace customer-service
```

Then check the new Pod logs:

```bash
kubectl logs deployment/cs-local-customer-service-app \
  --namespace customer-service
```

Or run the helper script:

```bash
scripts/deploy-local.sh
```

The script defaults to `cs-local`, `customer-service`, `customer-service:local`, and
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

### Knowledge base direction

The app no longer loads startup KB files from a mounted directory or ConfigMap. Tenant KB
storage and retrieval remain in place through the retrieval/vector store, but knowledge
should be populated by future source tools, cloud-document connectors, learned support
answers, or admin workflows rather than static files mounted into the pod.

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

For each incoming customer message, the graph first runs a question planner. The planner
receives the latest customer message, the optional sender name, and compact greeting
metadata derived from the minute delta since the previous customer message. It does not
receive full conversation history at this stage. The planner decides whether the question
is in scope for customer service and whether conversation history is needed. If a question
is out of scope, the graph uses the planner's `explanation` as the customer-facing reply
and does not call the answer-generation LLM. If the question is standalone, such as a
location or menu question, the graph skips loading history and answers from KB context
only. If the question depends on earlier messages, the graph loads exact messages for the
current conversation since `conversation.created_at`.

Loaded history is bounded by `AGENT_CONVERSATION_HISTORY_MAX_MESSAGES` so very long open
chats do not overfill the LLM context.

The graph also derives compact greeting metadata for the LLM. By default,
`AGENT_GREETING_LAPSE_MINUTES=60`, so the prompt tells the LLM to greet the customer on
their first message or when the previous customer message was at least 60 minutes ago.
Absolute timestamps are intentionally omitted from the prompt metadata.

The current prompt context is:

- derived conversation metadata;
- exact current conversation history with message `created_at` timestamps and readable
  relative ages, when the planner requests it;
- retrieved KB documents;
- the current customer question.

Conversation metadata is used only for greeting decisions. For questions such as "when
did I first send a message?", the answer prompt instructs the LLM to use the timestamped
conversation-history entries rather than the greeting metadata.
The conversation history block also describes its line format, chronological order, and
sender types so the LLM can identify the first customer message and use `created_at`
values for exact times. Relative ages such as `26 minutes ago` or `2 days ago` are
included as readability hints, but the exact timestamp remains the source of truth.

Raw messages remain the source of truth. Summarized conversation memory is intentionally
reserved for a later increment.

## Tenant foundation

The local MVP is still configured as a single running app, but inbound messages now carry
a `tenant_id`. When no tenant is supplied, the app uses `AGENT_DEFAULT_TENANT_ID`
(`tenant.defaultId` in Helm), which defaults to `default`.

Tenant isolation currently covers:

- tenant records with immutable generated `tenant_id` values and mutable readable slugs;
- conversations, scoped by `(tenant_id, channel, external_chat_id)`;
- message idempotency, scoped by `(tenant_id, event_id)`;
- seed KB retrieval namespaces, using `seed-knowledge` for the default tenant and
  `<tenant_id>:seed-knowledge` for other tenants;
- tenant prompt configuration for answer and planner instructions;
- LLM project metadata per tenant;
- LangSmith trace metadata per tenant, written into one deployment-level LangSmith
  project;
- one shared vector collection/index with per-tenant namespaces;
- opt-in tenant features, which can later be enforced by a prepaid credit balance;
  basic RAG and human handover are treated as inbuilt capabilities rather than opt-in
  features;
- selected onboarding plan reference, recorded as audit/control-plane context but not
  re-applied at runtime.

### Bruno tenant onboarding flow

The `bruno/onboarding` folder contains an ordered onboarding flow:

1. create the app tenant;
2. read the tenant back by slug and populate Bruno runtime variables;
3. create/update the tenant Telegram Kubernetes Secret;
4. register the tenant Telegram webhook;
5. create an OpenAI project with the OpenAI Admin API;
6. create/upsert the deployment-level LangSmith tracing project;
7. create the tenant config from the returned tenant/provider values;
8. read back the tenant config.

Before running it, select a Bruno environment and set these variables:

- `openai_admin_key`: OpenAI Admin API key for an organization owner;
- `langsmith_api_key`: LangSmith API key;
- `langsmith_workspace_id`: LangSmith workspace id targeted by the API key;
- `deployment_langsmith_project`: deployment-level LangSmith trace project, for example
  `customer-service-local`;
- `tenant_display_name`: business/customer display name;
- `tenant_enabled_features_json`: JSON array such as `["telegram","whatsapp"]`.
- `telegram_bot_token`: tenant Telegram bot token;
- `telegram_webhook_secret_token`: tenant Telegram webhook secret token;
- `telegram_webhook_base_url`: public HTTPS base URL for this app, without a trailing
  slash;
- `k8s_proxy_url`: local Kubernetes API proxy URL, for example
  `http://127.0.0.1:8001`;
- `k8s_namespace`: Kubernetes namespace where tenant Telegram Secrets should be created.

Before running the Kubernetes Secret request, start a local proxy:

```bash
kubectl proxy --port=8001 --address=127.0.0.1
```

The requests use Bruno runtime variables to pass values between steps. Tenant creation
stores only the generated slug for the explicit lookup step. The slug lookup stores
`tenant_id`, `tenant_slug`, `llm_project_name`, `langsmith_project`, and the slug-based
vector namespace. The Telegram Secret request stores `tenant_telegram_secret_name`; the
provider creation responses then store `llm_project_id`. LangSmith traces are written to
the deployment-level `LANGSMITH_PROJECT`, while `langsmith_project` remains tenant
metadata for filtering/audit.

Create a tenant record with:

```bash
curl -X POST http://localhost:8000/tenants \
  -H 'content-type: application/json' \
  -d '{
    "display_name": "Hustle HQ",
    "selected_plan": "sme"
  }'
```

The response includes an immutable generated tenant id such as `tnt_...`, a readable slug
such as `hustle-hq`, and no tenant config. Tenant config is created later with
`PUT /tenants/{tenant_id}/config`, after provider project names are known. Use the
generated `tenant_id` for chat routing, tenant config, and KB ingestion. The slug is for
display/search/URLs and may later be changed independently.

Tenant creation fails closed on duplicate slugs. If a matching derived or explicit slug
already exists, `POST /tenants` returns `409 Conflict` instead of returning someone
else's tenant id. Use the explicit lookup endpoint when you intentionally want an existing
tenant:

```bash
curl http://localhost:8000/tenants/by-slug/hustle-hq
```

Read the tenant record with:

```bash
curl http://localhost:8000/tenants/tnt_abc123...
```

For synthetic/API calls, include `tenant_id` in the JSON body or send
`X-Agent-Tenant-Id`. For Telegram and WhatsApp webhook posts, pass `tenant_id` as a
query parameter, for example `/webhooks/telegram?tenant_id=hustle-hq`, or send
`X-Agent-Tenant-Id`.

Tenant prompt configuration is managed through:

```bash
curl -X PUT http://localhost:8000/tenants/tnt_abc123.../config \
  -H 'content-type: application/json' \
  -d '{
    "selected_plan": "sme",
    "enabled_features": ["telegram", "whatsapp"],
    "answer_prompt_instructions": "Use Hustle HQ'\''s warm, concise brand voice.",
    "planner_prompt_instructions": "Questions about table bookings and private events are in scope.",
    "llm_project_id": "proj_hustle_hq",
    "llm_project_name": "customer-service-hustle-hq",
    "langsmith_project": "customer-service-hustle-hq",
    "llm_provider": "langchain-compatible",
    "llm_model": "deepseek-chat",
    "llm_base_url": "https://api.deepseek.com",
    "vector_provider": "pgvector",
    "vector_isolation_mode": "shared_collection",
    "vector_collection": "customer-service",
    "vector_namespace": "hustle-hq:seed-knowledge",
    "telegram_secret_name": "tenant-hustle-hq-telegram",
    "whatsapp_secret_name": "tenant-hustle-hq-whatsapp"
  }'
```

Read it back with:

```bash
curl http://localhost:8000/tenants/tnt_abc123.../config
```

Admin users, tenant memberships, authentication, and role checks are intentionally not part
of this increment. They will be added together with the tenant owner/admin ingestion flow,
so the same KB ingestion service can be protected by Keycloak/social-login-backed admin
authorization later.

These instructions are prompt overlays. The shared base prompts still apply and tenant
instructions must not override global safety, routing, grounding, or language rules.
Tenant config requests are handled by the tenant API router and validate known plans,
features, LLM providers, vector providers, vector isolation modes, and absolute HTTP(S)
LLM base URLs before persistence.

Tenant plans are reference templates for onboarding/deployment decisions. The initial Helm
values include `tenant.plans.sme` and `tenant.plans.enterprise` with suggested defaults
for features, vector-index mode, and hosting model. After onboarding, runtime behavior
comes from the resolved tenant fields such as `enabled_features`, `vector_collection`, and
`vector_namespace`; changing `selected_plan` later does not automatically re-apply a
template.

Tenant config reads use a read-through cache. Local Helm defaults to Redis so multiple app
replicas can share cached tenant config values. Updating a tenant through the tenant config
API refreshes the Redis entry, and the cache TTL provides a safety net for out-of-band
database changes. Tenant config cache keys use this pattern:
`tenant-config:<tenant_id>`.

Provider project/index fields are tenant control-plane metadata:

- OpenAI uses one shared provider API key, with project metadata stored per tenant for
  onboarding/client construction.
- The `llm_project_id` and `llm_project_name` field names are intentionally kept stable
  even when the tenant uses another LangChain-compatible provider. For providers that use
  a different grouping term, map the provider's nearest equivalent into these fields; for
  example, a workspace, account scope, deployment group, billing project, or tenant-owned
  provider project. The bot treats these values as metadata and trace/client-construction
  hints, not as OpenAI-specific concepts.
- LangSmith traces are written to the deployment-level `LANGSMITH_PROJECT` and tagged
  with tenant metadata. The tenant config's `langsmith_project` value is retained as
  metadata/filtering context, not as the runtime trace destination.
- Vector storage is modeled generically using `vector_provider`,
  `vector_isolation_mode`, `vector_collection`, and `vector_namespace`. Local Helm
  configures the default collection with `AGENT_VECTOR_COLLECTION`; Pinecone can map
  collection to index and namespace to namespace, while Qdrant can map collection to
  collection and namespace/tenant to payload filters or a dedicated collection.
- Telegram credentials can be resolved from one Secret reference per tenant. Secret values
  are not stored in Postgres; `telegram_secret_name` points at the Kubernetes Secret used
  for that tenant's bot token and webhook secret token. If no tenant Secret is configured,
  the app falls back to the global Telegram env/Helm Secret values.
- WhatsApp credentials are currently modeled as one Secret reference per tenant, but the
  runtime still uses the global WhatsApp env/Helm Secret values until tenant-specific
  WhatsApp resolution is wired.

Telegram tenant Secret keys:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET_TOKEN
```

WhatsApp tenant Secret keys:

```text
WHATSAPP_ACCESS_TOKEN
WHATSAPP_PHONE_NUMBER_ID
WHATSAPP_VERIFY_TOKEN
WHATSAPP_GRAPH_API_VERSION
```

Feature enforcement, prepaid credit checks, support backend selection, admin KB ingestion,
provider project creation jobs, and usage tracking are reserved for later increments.

## Optional LLM answer provider

The default answer provider is still deterministic and local:

```env
AGENT_ANSWER_PROVIDER=extractive
AGENT_QUESTION_PLANNER_PROVIDER=rules
AGENT_HUMAN_REQUEST_DETECTOR_PROVIDER=rules
```

Run the API locally with the OpenAI answer provider:

```bash
OPENAI_API_KEY="$OPENAI_API_KEY" scripts/run-local.sh
```

To use an OpenAI-backed LangChain chat model with langsmith tracing, create a Kubernetes secret containing the API
keys:

```bash
kubectl create secret generic api-keys \
  --namespace customer-service \
  --from-literal=OPENAI_API_KEY="key" \
  --from-literal=key="key"
```

Then deploy on kubernetes with:

```bash
helm upgrade --install cs-local helm/customer-service \
  --namespace customer-service \
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
If a tenant config has `telegram_secret_name`, the app reads that Kubernetes Secret,
validates the incoming `X-Telegram-Bot-Api-Secret-Token` against the tenant-specific
`TELEGRAM_WEBHOOK_SECRET_TOKEN`, and sends the reply with the tenant-specific
`TELEGRAM_BOT_TOKEN`. If no tenant Secret is configured, the app falls back to
`AGENT_TELEGRAM_BOT_TOKEN` and `AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN`.

Create a Secret for the bot token and webhook secret token:

```bash
kubectl create secret generic telegram-bot \
  --namespace customer-service \
  --from-literal=TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
  --from-literal=TELEGRAM_WEBHOOK_SECRET_TOKEN="$TELEGRAM_WEBHOOK_SECRET_TOKEN"
```

Deploy with the Secret:

```bash
helm upgrade --install cs-local helm/customer-service \
  --namespace customer-service \
  --set telegram.existingSecret=telegram-bot
```

For tenant-specific Telegram credentials, create a Secret with the same keys and store its
name in the tenant config. The Bruno onboarding flow can create this Secret through
`kubectl proxy`; manually, it looks like:

```bash
kubectl create secret generic tenant-hustle-hq-telegram \
  --namespace customer-service \
  --from-literal=TELEGRAM_BOT_TOKEN="$TENANT_TELEGRAM_BOT_TOKEN" \
  --from-literal=TELEGRAM_WEBHOOK_SECRET_TOKEN="$TENANT_TELEGRAM_WEBHOOK_SECRET_TOKEN"
```

```json
{
  "telegram_secret_name": "tenant-hustle-hq-telegram"
}
```

Local Helm enables `telegram.credentialProvider=kubernetes`, so the app ServiceAccount is
allowed to read tenant Telegram Secrets in its namespace. Use
`telegram.credentialProvider=static` for env-only local runs.

Register the Telegram webhook after the app has a public HTTPS URL:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -H 'content-type: application/json' \
  -d '{
    "url": "https://example.com/webhooks/telegram",
    "secret_token": "'"$TELEGRAM_WEBHOOK_SECRET_TOKEN"'"
  }'
```

## WhatsApp customer webhook

WhatsApp customer messages are received at:

```text
GET /webhooks/whatsapp
POST /webhooks/whatsapp
```

The `GET` endpoint verifies the Meta WhatsApp webhook using
`AGENT_WHATSAPP_VERIFY_TOKEN`. The `POST` endpoint currently handles customer text
messages from the WhatsApp Cloud API payload. Non-text updates are acknowledged and
ignored. If `AGENT_WHATSAPP_ACCESS_TOKEN` and `AGENT_WHATSAPP_PHONE_NUMBER_ID` are
configured, the app sends the graph reply back to the customer using the WhatsApp Cloud
API `messages` endpoint.

Create a Secret for the WhatsApp Cloud API values:

```bash
kubectl create secret generic whatsapp-cloud \
  --namespace customer-service \
  --from-literal=WHATSAPP_ACCESS_TOKEN="$WHATSAPP_ACCESS_TOKEN" \
  --from-literal=WHATSAPP_PHONE_NUMBER_ID="$WHATSAPP_PHONE_NUMBER_ID" \
  --from-literal=WHATSAPP_VERIFY_TOKEN="$WHATSAPP_VERIFY_TOKEN"
```

Deploy with the Secret:

```bash
helm upgrade --install cs-local helm/customer-service \
  --namespace customer-service \
  --set whatsapp.existingSecret=whatsapp-cloud
```

Register the WhatsApp webhook in Meta's app dashboard after the app has a public HTTPS URL:

```text
https://example.com/webhooks/whatsapp
```

## Configuration

Application configuration uses the `AGENT_` prefix. LangSmith uses its native
`LANGSMITH_` names:

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_DEFAULT_TENANT_ID` | `default` | Tenant id used when an inbound message does not explicitly provide one |
| `AGENT_RETRIEVAL_PROVIDER` | `memory` | `memory` or `pgvector` |
| `AGENT_ANSWER_PROVIDER` | `extractive` | `extractive` or `openai` |
| `AGENT_EMBEDDING_PROVIDER` | `local` | `local` or `openai`; Helm defaults to `openai` for pgvector |
| `AGENT_EMBEDDING_MODEL` | `text-embedding-3-small` | OpenAI embedding model when `AGENT_EMBEDDING_PROVIDER=openai` |
| `AGENT_EMBEDDING_DIMENSIONS` | `64` | pgvector embedding size; Helm defaults to `1536` for OpenAI embeddings |
| `AGENT_QUESTION_PLANNER_PROVIDER` | `rules` | `rules` or `llm`; planner receives the latest customer message plus compact greeting metadata and decides scope/history routing |
| `AGENT_HUMAN_REQUEST_DETECTOR_PROVIDER` | `rules` | `rules` or `llm`; `llm` uses OpenAI to detect explicit human-agent requests |
| `OPENAI_API_KEY` | unset | Required when `AGENT_ANSWER_PROVIDER=openai`, `AGENT_EMBEDDING_PROVIDER=openai`, `AGENT_QUESTION_PLANNER_PROVIDER=llm`, or `AGENT_HUMAN_REQUEST_DETECTOR_PROVIDER=llm` |
| `AGENT_LLM_MODEL` | `gpt-4.1-mini` | OpenAI chat model used by the LLM answer provider |
| `AGENT_LLM_TEMPERATURE` | `0.0` | LLM sampling temperature |
| `AGENT_DATABASE_URL` | unset | PostgreSQL connection string |
| `AGENT_CONFIDENCE_THRESHOLD` | `0.60` | Below this, mark the response as low confidence |
| `AGENT_CONVERSATION_HISTORY_MAX_MESSAGES` | `50` | Safety cap for exact current-conversation messages passed into context |
| `AGENT_GREETING_LAPSE_MINUTES` | `60` | Minutes after the previous customer message before the prompt says to greet again |
| `AGENT_TENANT_CONFIG_CACHE_PROVIDER` | `memory` | Tenant config cache provider: `memory` or `redis`; Helm defaults to `redis` |
| `AGENT_TENANT_CONFIG_CACHE_TTL_SECONDS` | `300` | TTL for Redis tenant config cache entries |
| `AGENT_REDIS_URL` | unset | Redis URL required when `AGENT_TENANT_CONFIG_CACHE_PROVIDER=redis`; Helm points this at the bundled Redis service |
| `AGENT_VECTOR_COLLECTION` | `customer-service` | Default vector collection/index name used by tenant config defaults; tenant namespaces isolate data |
| `AGENT_TELEGRAM_BOT_TOKEN` | unset | Telegram bot token used to send replies with `sendMessage` |
| `AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN` | unset | Optional Telegram webhook secret token checked against `X-Telegram-Bot-Api-Secret-Token` |
| `AGENT_TELEGRAM_CREDENTIAL_PROVIDER` | `static` | `static` for env-backed Telegram credentials, or `kubernetes` for tenant-specific Secret lookup |
| `AGENT_TELEGRAM_SECRET_NAMESPACE` | unset | Kubernetes namespace used for tenant Telegram Secret lookup; Helm defaults this to the pod namespace |
| `AGENT_TELEGRAM_BOT_TOKEN_SECRET_KEY` | `TELEGRAM_BOT_TOKEN` | Secret key containing a tenant Telegram bot token |
| `AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN_SECRET_KEY` | `TELEGRAM_WEBHOOK_SECRET_TOKEN` | Secret key containing a tenant Telegram webhook secret token |
| `AGENT_WHATSAPP_ACCESS_TOKEN` | unset | WhatsApp Cloud API access token used to send replies |
| `AGENT_WHATSAPP_PHONE_NUMBER_ID` | unset | WhatsApp Cloud API phone number id used for outbound messages |
| `AGENT_WHATSAPP_VERIFY_TOKEN` | unset | WhatsApp webhook verification token checked during Meta webhook setup |
| `AGENT_WHATSAPP_GRAPH_API_VERSION` | `v20.0` | Meta Graph API version used for WhatsApp outbound messages |
| `AGENT_LOG_LEVEL` | `INFO` | Application log level, for example `DEBUG` |
| `AGENT_LOG_FORMAT` | `{asctime} - {levelname}:{name}:{message}` | Python logging format using `{}` style |
| `LANGSMITH_TRACING` | `true` | Enable LangSmith tracing |
| `LANGSMITH_TRACING_V2` | `true` | Enable LangSmith tracing v2 |
| `LANGCHAIN_TRACING_V2` | `true` | Legacy LangChain tracing v2 env var kept for SDK compatibility |
| `LANGSMITH_ENDPOINT` | `https://eu.api.smith.langchain.com` | LangSmith endpoint |
| `LANGSMITH_PROJECT` | `customer-service-local` | Deployment-level LangSmith trace project; tenant identity is represented with tags/metadata |
| `LANGSMITH_WORKSPACE_ID` | unset | LangSmith workspace id; required for org-scoped API keys or keys linked to multiple workspaces |

Changing `AGENT_EMBEDDING_DIMENSIONS` changes the required pgvector column type. Use a
fresh database, recreate the `knowledge_documents` table, or reindex the KB when moving
between local 64-dimensional embeddings and OpenAI 1536-dimensional embeddings.
Knowledge rows use stable chunk ids such as `kb/menu.txt#0000` so callers can trace
answers to exact retrieved chunks.
API response citations return these chunk ids, not just source file paths, so callers can
trace an answer to the exact retrieved chunk.
Retrieved chunks passed to the LLM include `chunk_id`, `source`, and the pgvector
`created_at` timestamp for that chunk.
The LLM prompt instructs the model to prefer newer `created_at` chunks only when multiple
relevant chunks overlap or conflict; retrieval ranking remains semantic-only.

## Increment 4 boundary

A `low_confidence: true` response indicates low answer confidence but does not persist a
handoff state. The graph can move a conversation to `HUMAN_REQUESTED` when the customer
explicitly asks for a human agent, but creating/reusing Telegram or WhatsApp support
groups, tracking internal agent discussion, deciding which agent messages to forward, and
forwarding those messages to the customer still belong to later increments.
