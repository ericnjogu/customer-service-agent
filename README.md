# Customer Service Agent

Increment 4 is a locally runnable vertical slice using FastAPI, LangGraph, LangChain
documents, tenant-scoped KB retrieval, conversation routing state, optional LLM-backed answer
generation, and configurable retrieval/answer boundaries. Helm deploys the service with
PostgreSQL and pgvector. No external LLM key is needed for the default local path: a
deterministic extractive generator and local hash embeddings make the workflow inspectable
and reproducible.
f
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
  out-of-scope questions, decides whether conversation history is needed, detects
  explicit human-agent requests, and uses greeting metadata for out-of-scope explanations.
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

### React onboarding wizard

The Helm chart can also deploy the React/Vite onboarding wizard as a separate web service.
The wizard talks to FastAPI for validation, draft state, website analysis, Telegram setup
tokens, and final provisioning.

Website analysis is LLM-backed, with a faster first pass before the final model call.
FastAPI fetches the submitted website directly and extracts exact contact links such as
social profiles, Google Maps links, WhatsApp links, email addresses, and telephone links.
If `AGENT_PLATFORM_WEB_SEARCH_PROVIDER=tavily`, FastAPI also uses Tavily to collect
concise business-profile, location, service, and contact notes under the configured
platform project id. The extracted links and Tavily notes are then sent to OpenAI to
produce the editable business profile, agent name, agent
description, prompt instructions, and contact information. `OPENAI_API_KEY` must be
configured before `POST /onboarding/sessions/{session_id}/analyze-website` can complete
when `onboarding.websiteAnalysisProvider=openai`.

To tune the live hybrid website analysis flow, run the opt-in integration test:

```bash
AGENT_RUN_LIVE_OPENAI_TESTS=true \
OPENAI_API_KEY="$OPENAI_API_KEY" \
AGENT_PLATFORM_WEB_SEARCH_PROVIDER=tavily \
AGENT_PLATFORM_WEB_SEARCH_API_KEY="$TAVILY_API_KEY" \
AGENT_PLATFORM_WEB_SEARCH_PROJECT_ID=ristoh-css \
OPENAI_WEBSITE_RESEARCH_URL="https://ristoh.co.ke/" \
OPENAI_WEBSITE_RESEARCH_EXPECTED_SOCIAL_URLS="facebook.com" \
uv run pytest tests/test_onboarding_website_research_live.py -q -s --log-cli-level=INFO
```

To compare that against OpenAI Responses API web discovery on its own, run the separate
business-profile discovery test:

```bash
AGENT_RUN_LIVE_OPENAI_TESTS=true \
OPENAI_API_KEY="$OPENAI_API_KEY" \
OPENAI_RESPONSES_BUSINESS_PROFILE_URL="https://ristoh.co.ke/" \
OPENAI_RESPONSES_BUSINESS_PROFILE_EXPECTED_TERMS="ristoh,customer service" \
uv run pytest tests/test_openai_responses_business_profile_live.py -q -s --log-cli-level=INFO
```

For local development:

```bash
cd web
npm install
npm run dev
```

Then open http://localhost:5173. Vite proxies `/api/*` to FastAPI at
`http://localhost:8000`, so the browser does not need CORS for wizard API calls.

Run the React form tests with:

```bash
cd web
npm test
```

For Kubernetes:

```bash
kubectl port-forward service/support-customer-service-app 8000:8000 \
  --namespace customer-service
kubectl port-forward service/support-customer-service-web 5173:8080 \
  --namespace customer-service
```

The local deploy script builds both images. Use these variables when the browser-facing
URLs differ from the defaults:

```bash
WEB_PUBLIC_BASE_URL=http://localhost:5173 \
WEB_API_BASE_URL=/api \
./scripts/deploy-local.sh
```

By default, onboarding requires the admin email domain to match the website domain. For
local demos or cases where the business owner uses a different email domain, disable that
validation with:

```bash
AGENT_ONBOARDING_REQUIRE_ADMIN_EMAIL_DOMAIN_MATCH=false \
./scripts/deploy-local.sh
```

To enable Tavily during local Kubernetes deploys, create or update a Secret containing
the shared platform Tavily API key, then pass the Secret reference and platform project
id to Helm through the deploy script:

```bash
kubectl create secret generic api-keys \
  --namespace customer-service \
  --from-literal=PLATFORM_TAVILY_API_KEY="$TAVILY_API_KEY" \
  --dry-run=client \
  -o yaml | kubectl apply -f -

AGENT_PLATFORM_WEB_SEARCH_PROVIDER=tavily \
AGENT_PLATFORM_WEB_SEARCH_PROJECT_ID=ristoh-css \
AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_NAME=api-keys \
AGENT_PLATFORM_WEB_SEARCH_API_KEY_SECRET_KEY=PLATFORM_TAVILY_API_KEY \
./scripts/deploy-local.sh
```

The web nginx container proxies `/api/*` to the FastAPI Service inside Kubernetes.
FastAPI CORS is still configured for direct API calls, but the wizard normally uses
same-origin `/api` requests. For an ngrok test, set the web public URL to the
browser-facing tunnel URL:

```bash
WEB_PUBLIC_BASE_URL=https://your-web-tunnel.ngrok-free.app \
WEB_API_BASE_URL=/api \
AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL=https://your-web-tunnel.ngrok-free.app/api \
./scripts/deploy-local.sh
```

The app exposes these wizard APIs:

- `POST /onboarding/sessions`
- `GET /onboarding/sessions/{session_id}`
- `PATCH /onboarding/sessions/{session_id}`
- `PATCH /onboarding/sessions/{session_id}/website`
- `POST /onboarding/sessions/{session_id}/send-username-email-verification`
- `POST /onboarding/sessions/{session_id}/verify-username-email`
- `POST /onboarding/sessions/{session_id}/send-website-email-verification`
- `POST /onboarding/sessions/{session_id}/verify-website-email`
- `POST /onboarding/sessions/{session_id}/analyze-website`
- `POST /onboarding/sessions/{session_id}/request-telegram-setup`
- `POST /onboarding/sessions/{session_id}/telegram-setup`
- `POST /onboarding/sessions/{session_id}/submit`

The onboarding wizard now verifies two separate addresses. The account setup step
sends a real inbox-verification email to `username_email`, which is the future
dashboard login identity. After that link is opened, the website verification step
collects `website_url` and `website_verification_email`, then sends a second inbox
verification email to prove control of the business website/contact domain. Website
analysis, Telegram setup, and final submit are blocked by FastAPI until both
`username_email_verified=true` and `website_email_verified=true`.

FastAPI also sends the final onboarding confirmation email after provisioning succeeds.
The confirmation goes to the tenant admin and to `AGENT_ONBOARDING_REVIEW_EMAIL` when
configured.

FastAPI also sends the SaaS-admin Telegram setup email. The email contains a signed
one-time React setup link and an onboarding summary so the SaaS admin can review the
tenant details before entering Telegram credentials.

For local logging-only email behavior, keep the default:

```bash
AGENT_EMAIL_PROVIDER=log ./scripts/deploy-local.sh
```

For real Resend delivery from FastAPI:

```bash
kubectl create secret generic app-email \
  --namespace customer-service \
  --from-literal=RESEND_API_KEY="re_..."

AGENT_EMAIL_PROVIDER=resend \
AGENT_EMAIL_FROM=onboarding@example.com \
AGENT_ONBOARDING_REVIEW_EMAIL=onboarding-review@example.com \
AGENT_EMAIL_RESEND_API_KEY_SECRET_NAME=app-email \
./scripts/deploy-local.sh
```

The deploy script uses the same `AGENT_EMAIL_*` names as the FastAPI runtime. It passes
`AGENT_EMAIL_FROM` to Helm as `email.from`, and the Helm template renders it into the
FastAPI Pod as `AGENT_EMAIL_FROM`.
`AGENT_ONBOARDING_REVIEW_EMAIL` is the internal/SaaS review recipient for Telegram setup,
failure, and final confirmation notifications.

### Local n8n workflow service

The n8n chart is retained for local workflow experiments, but it is disabled by default
and is no longer required for the onboarding wizard. The React/FastAPI app now handles
tenant-admin email verification, SaaS-admin Telegram setup email, provisioning, and final
confirmation email.

If you explicitly enable n8n, it uses the bundled PostgreSQL service for workflows,
credentials metadata, and executions. It still mounts a PVC at `/home/node/.n8n` for n8n
local files such as encryption/settings data.

Enable n8n locally with:

```bash
N8N_ENABLED=true ./scripts/deploy-local.sh
```

Open n8n locally with:

```bash
kubectl port-forward service/support-customer-service-n8n 5678:5678 \
  --namespace customer-service
```

Then open http://localhost:5678.

To skip the n8n setup wizard in local Kubernetes, create a Secret containing a bcrypt
password hash for the owner account. Do not store the password or hash in this repo.

```bash
N8N_OWNER_PASSWORD_HASH="$(
  htpasswd -bnBC 10 "" "local-only-password" |
    tr -d ':\n' |
    sed 's/^\$2y\$/\$2a\$/'
)"
N8N_ENCRYPTION_KEY="$(openssl rand -hex 32)"

kubectl create secret generic n8n-owner \
  --namespace customer-service \
  --from-literal=N8N_INSTANCE_OWNER_PASSWORD_HASH="${N8N_OWNER_PASSWORD_HASH}" \
  --from-literal=N8N_ENCRYPTION_KEY="${N8N_ENCRYPTION_KEY}"
```

If you want to paste the generated hash directly into a shell script or wrapper as an
environment variable, escape the `$` characters first:

```bash
htpasswd -bnBC 10 "" "local-only-password" |
  tr -d ':\n' |
  sed 's/^\$2y\$/\$2a\$/' |
  sed 's/\$/\\$/g'
```

Then enable owner provisioning in Helm:

```bash
helm upgrade --install support helm/customer-service \
  --namespace customer-service \
  --set n8n.enabled=true \
  --set n8n.owner.managedByEnv=true \
  --set n8n.owner.email=admin@example.com \
  --set n8n.owner.firstName=Local \
  --set n8n.owner.lastName=Admin \
  --set n8n.owner.existingSecret=n8n-owner \
  --set n8n.encryptionKey.existingSecret=n8n-owner
```

Or use the local deploy script with the same configuration:

```bash
N8N_OWNER_MANAGED_BY_ENV=true \
N8N_OWNER_EMAIL=admin@example.com \
N8N_OWNER_FIRST_NAME=Local \
N8N_OWNER_LAST_NAME=Admin \
N8N_OWNER_SECRET_NAME=n8n-owner \
N8N_ONBOARDING_EMAIL=onboarding@example.com \
N8N_ENABLED=true \
./scripts/deploy-local.sh
```

The deploy script does not create or update the n8n owner Secret. It expects the Secret
named by `N8N_OWNER_SECRET_NAME` to already exist, then passes that Secret name into Helm.
It also passes `N8N_ONBOARDING_EMAIL` to n8n as `ONBOARDING_EMAIL`. The Resend API key
Secret defaults to `api-keys` / `RESEND_API_KEY`; override it with
`N8N_EMAIL_RESEND_API_KEY_SECRET_NAME` and `N8N_EMAIL_RESEND_API_KEY_SECRET_KEY` if needed.

If you already completed the setup wizard, reset n8n user management first. This removes
n8n user accounts, but keeps workflows, credentials, and execution history in the
database.

```bash
kubectl exec -n customer-service deploy/support-customer-service-n8n -- \
  n8n user-management:reset

kubectl rollout restart -n customer-service deployment/support-customer-service-n8n
```

If n8n needs to receive callbacks from external services, expose it through a tunnel and
set the public URL during deployment:

```bash
helm upgrade --install support helm/customer-service \
  --namespace customer-service \
  --set n8n.webhookUrl=https://example.ngrok-free.app/ \
  --set n8n.editorBaseUrl=https://example.ngrok-free.app/
```

For the initial website onboarding flow design, see
[local-docs/initial-website.md](local-docs/initial-website.md). The previous importable
n8n workflow has been removed from the runtime path; use n8n only if a future experiment
needs an external workflow wrapper.

If an experimental n8n workflow sends email through Resend, the n8n Pod can still receive
`RESEND_API_KEY` and `ONBOARDING_EMAIL` from Helm:

```bash
kubectl create secret generic n8n-email \
  --namespace customer-service \
  --from-literal=RESEND_API_KEY="re_..."

helm upgrade --install support helm/customer-service \
  --namespace customer-service \
  --set n8n.enabled=true \
  --set n8n.email.resendApiKeySecretName=n8n-email \
  --set n8n.email.onboardingEmail=onboarding@example.com
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
storage and retrieval remain in place through the retrieval/vector store. Initial tenant
knowledge is created by onboarding jobs from reviewed business details and URL-backed
website research snippets. Additional knowledge should come from future source tools,
cloud-document connectors, learned support answers, or admin workflows rather than static
files mounted into the pod.

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
is in scope for customer service, whether conversation history is needed, and whether the
customer explicitly requested a human agent. If a question
is out of scope, the graph uses the planner's `explanation` as the customer-facing reply
and does not call the answer-generation LLM. If the question is standalone, such as a
location or menu question, the graph skips loading history and answers from KB context
only. If the question depends on earlier messages, the graph loads exact messages for the
current conversation since `conversation.created_at`.

Loaded history is bounded by `AGENT_CONVERSATION_HISTORY_MAX_MESSAGES` so very long open
chats do not overfill the LLM context.
Customer messages that the planner marks out of scope, and the corresponding bot replies,
are still persisted with `in_scope=false` for audit/debugging, but they are excluded from
future conversation-history context sent to the LLM.

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
- tenant-scoped KB retrieval namespaces, using `default` for the default tenant and
  tenant slugs such as `<tenant_id>` for other tenants;
- knowledge grouping via document metadata such as `source_type`, so one tenant
  namespace can hold onboarding profile docs, website snippets, uploads, and other
  future source types;
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
3. optionally create/update the tenant Telegram Kubernetes Secret manually;
4. optionally register the tenant Telegram webhook manually;
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
- Telegram webhook secret tokens are generated by the app during onboarding, equivalent
  to `openssl rand -hex 32`;
- `telegram_webhook_base_url`: public HTTPS base URL for this app, without a trailing
  slash; the app normally uses `AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL` during
  onboarding job processing, while this Bruno variable remains useful for manual
  webhook debugging;
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
vector namespace. The app onboarding job derives and creates/updates the Telegram Secret
name `tenant-<tenant-slug>-telegram`; the Bruno Telegram Secret and webhook requests are
kept only as manual debugging utilities. Provider creation responses then store
`llm_project_id`. Runtime OpenAI/LangChain calls use `llm_project_id` as the
`OpenAI-Project` header when it is present, and LangSmith traces are written to the
tenant `langsmith_project` when configured. If tenant project values are absent, the app
falls back to the deployment-level provider defaults.

### Website onboarding workflow

Website onboarding is now driven by the React wizard and FastAPI session APIs. FastAPI
validates the start form, sends the tenant-admin inbox verification email, persists draft
wizard state, prepares website-analysis draft data, accepts reviewed details, generates
and emails the SaaS-admin Telegram setup link, and submits the completed session into the
onboarding job flow after the SaaS admin submits Telegram fields through the React setup
page. Provider project creation/filling is owned by the onboarding job before tenant
config is written, so Telegram setup, direct job submissions, and explicit session
submit retries all use the same boundary. After the job succeeds, FastAPI sends final
confirmation email to both the tenant admin and the configured onboarding review email
address.

The onboarding job also creates the tenant's initial KB entries in the tenant vector
namespace. It stores an approved onboarding profile/contact/instructions document plus
URL-backed website research snippets gathered during analysis. URL-backed chunks keep
their `source_url`, provider, retrieval timestamp, and onboarding session id in metadata
so later retrieval can cite and audit where the information came from.

Provider project provisioning defaults to API-backed behavior in Helm and in
`scripts/deploy-local.sh`. To create/get provider projects during onboarding job
processing, provide an OpenAI Admin API key and LangSmith credentials in the configured
Secret:

```bash
kubectl create secret generic api-keys \
  --namespace customer-service \
  --from-literal=OPENAI_ADMIN_KEY="sk-admin-..." \
  --from-literal=LANGSMITH_API_KEY="lsv2_..." \
  --from-literal=LANGSMITH_WORKSPACE_ID="..." \
  --dry-run=client \
  -o yaml | kubectl apply -f -

AGENT_PROVIDER_PROJECT_PROVISIONER=api \
AGENT_OPENAI_ADMIN_KEY_SECRET_NAME=api-keys \
AGENT_OPENAI_ADMIN_KEY_SECRET_KEY=OPENAI_ADMIN_KEY \
./scripts/deploy-local.sh
```

The LangSmith API key, endpoint, and workspace id use the existing LangSmith env/Secret
settings. To run without external provider admin API calls, set
`AGENT_PROVIDER_PROJECT_PROVISIONER=metadata`; in that mode the job only generates and
persists provider-facing project names. With API provisioning enabled, the app:

1. lists active OpenAI projects and reuses a matching project name when found;
2. creates the OpenAI project when missing and stores the returned `llm_project_id`;
3. creates/upserts the LangSmith project;
4. saves `llm_project_id`, `llm_project_name`, `langsmith_project`, and Tavily
   web-search metadata on tenant config during onboarding job processing.

Tavily uses the shared platform API key for onboarding website research and runtime
fallback. Runtime fallback is enabled by default with
`AGENT_RUNTIME_WEB_SEARCH_PROVIDER=tavily`; set it to `none` to disable runtime search.
If the platform Tavily API key is not configured, runtime fallback logs a warning and
acts as disabled. For in-scope questions, Tavily is called when the KB/history answer has
`answer_found=false`, `grounded=false`, or confidence below
`AGENT_CONFIDENCE_THRESHOLD`. Runtime Tavily requests use `web_search_project_name`
from tenant config as the Tavily `X-Project-ID`, falling back to the tenant id. When a
tenant has a website contact point, the runtime search also passes that website domain
to Tavily as `include_domains` so fallback answers stay anchored to the tenant's own
site. The LangGraph flow models this as an explicit `search_tenant_website` node after
KB answer generation. No tenant Tavily API key or Kubernetes Secret is created.

The onboarding job runs provider-project provisioning idempotently before tenant config
is written. If the project fields already exist, they are reused; if they are missing,
the job fills or creates them before saving tenant config. Transient OpenAI/LangSmith
network failures are retried with bounded exponential backoff and jitter before the job
is marked failed.

If the initial provisioning API call fails at the network layer, retry the same
`POST /admin/onboarding/jobs` request with the same `idempotency_key`. If the app accepts
the job but provisioning fails, retry it at
`POST /admin/onboarding/jobs/{job_id}/retry`. Retry is only allowed for failed jobs. The
retry endpoint reloads the persisted job payload, accepts only
`telegram.bot_token` in the retry body, generates a fresh webhook secret token, resets
the failed job to `accepted`, and schedules processing again.

The workflow design is tracked in
[local-docs/initial-website.md](local-docs/initial-website.md).

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
    "vector_namespace": "hustle-hq",
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

- OpenAI uses one shared provider API key. When `llm_project_id` is present on tenant
  config, runtime OpenAI/LangChain calls include it as the `OpenAI-Project` header so
  usage is attributed to the tenant project.
- The `llm_project_id` and `llm_project_name` field names are intentionally kept stable
  even when the tenant uses another LangChain-compatible provider. For providers that use
  a different grouping term, map the provider's nearest equivalent into these fields; for
  example, a workspace, account scope, deployment group, billing project, or tenant-owned
  provider project. The bot treats these values as metadata and trace/client-construction
  hints, not as OpenAI-specific concepts.
- LangSmith traces are written to the tenant config's `langsmith_project` when present
  and tagged with tenant metadata. If a tenant project is missing, LangSmith falls back
  to the deployment-level `LANGSMITH_PROJECT`.
- Tavily web-search metadata uses the shared platform API key and stores
  `web_search_provider` plus `web_search_project_name` on tenant config. Runtime
  fallback sends the project name as Tavily's project id when enabled. No tenant
  web-search Secret is created.
- Vector storage is modeled generically using `vector_provider`,
  `vector_isolation_mode`, `vector_collection`, and `vector_namespace`. Local Helm
  configures the default collection with `AGENT_VECTOR_COLLECTION`; Pinecone can map
  collection to index and namespace to namespace, while Qdrant can map collection to
  collection and namespace/tenant to payload filters or a dedicated collection.
- Telegram credentials can be resolved from one Secret reference per tenant. Secret values
  are not stored in Postgres; `telegram_secret_name` points at the Kubernetes Secret used
  for that tenant's bot token and webhook secret token. When
  `AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL` is configured, onboarding job processing also
  registers the tenant bot webhook with Telegram using the job's Telegram token and
  webhook secret token.
- WhatsApp credentials can be resolved from one Secret reference per tenant. Secret values
  are not stored in Postgres; `whatsapp_secret_name` points at the Kubernetes Secret used
  for that tenant's access token, phone number id, webhook verify token, and optional
  Graph API version.

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
  --from-literal=LANGSMITH_API_KEY="key" \
  --from-literal=LANGSMITH_WORKSPACE_ID="key"
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
Telegram credentials are tenant-scoped. If a tenant config has `telegram_secret_name`,
the app reads that Kubernetes Secret, validates the incoming
`X-Telegram-Bot-Api-Secret-Token` against the tenant-specific
`TELEGRAM_WEBHOOK_SECRET_TOKEN`, and sends the reply with the tenant-specific
`TELEGRAM_BOT_TOKEN`. If no tenant Secret is configured, the webhook is still processed
but no Telegram reply is sent.

Create a tenant Secret using the system-derived name `tenant-<tenant-slug>-telegram`.
The onboarding job stores that derived reference in the tenant config. The Bruno
onboarding flow can create this Secret through `kubectl proxy`; manually, it looks like:

```bash
kubectl create secret generic tenant-hustle-hq-telegram \
  --namespace customer-service \
  --from-literal=TELEGRAM_BOT_TOKEN="$TENANT_TELEGRAM_BOT_TOKEN" \
  --from-literal=TELEGRAM_WEBHOOK_SECRET_TOKEN="$TENANT_TELEGRAM_WEBHOOK_SECRET_TOKEN"
```

Local Helm enables `telegram.credentialProvider=kubernetes`, so the app ServiceAccount is
allowed to read tenant Telegram Secrets in its namespace.

If `AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL` / `telegram.webhookPublicBaseUrl` is set,
the onboarding job registers the Telegram webhook automatically:

```bash
AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL=https://your-web-tunnel.ngrok-free.app/api \
./scripts/deploy-local.sh
```

Use a direct API base such as `https://api.example.com`, or a web-proxied API base such
as `https://onboarding.example.com/api`. Do not use `WEB_API_BASE_URL` for this unless
it is an absolute public URL; local values often set it to the browser-only fragment
`/api`.

For manual debugging, register the Telegram webhook after the app has a public HTTPS URL:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -H 'content-type: application/json' \
  -d '{
    "url": "https://example.com/webhooks/telegram?tenant_id=tnt_...",
    "secret_token": "'"$TELEGRAM_WEBHOOK_SECRET_TOKEN"'"
  }'
```

## WhatsApp customer webhook

WhatsApp customer messages are received at:

```text
GET /webhooks/whatsapp
POST /webhooks/whatsapp
```

The `GET` endpoint verifies the Meta WhatsApp webhook using the tenant-specific
`WHATSAPP_VERIFY_TOKEN`. The `POST` endpoint currently handles customer text messages
from the WhatsApp Cloud API payload. Non-text updates are acknowledged and ignored. If
the resolved tenant Secret contains `WHATSAPP_ACCESS_TOKEN` and
`WHATSAPP_PHONE_NUMBER_ID`, the app sends the graph reply back to the customer using the
WhatsApp Cloud API `messages` endpoint.

Create a tenant Secret for the WhatsApp Cloud API values and store its name in the
tenant config:

```bash
kubectl create secret generic tenant-hustle-hq-whatsapp \
  --namespace customer-service \
  --from-literal=WHATSAPP_ACCESS_TOKEN="$WHATSAPP_ACCESS_TOKEN" \
  --from-literal=WHATSAPP_PHONE_NUMBER_ID="$WHATSAPP_PHONE_NUMBER_ID" \
  --from-literal=WHATSAPP_VERIFY_TOKEN="$WHATSAPP_VERIFY_TOKEN"
```

```json
{
  "whatsapp_secret_name": "tenant-hustle-hq-whatsapp"
}
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
| `AGENT_KB_CHUNK_SIZE` | `1000` | Maximum character size for recursive KB chunks |
| `AGENT_KB_CHUNK_OVERLAP` | `180` | Character overlap for recursive chunk splitting; must be smaller than chunk size |
| `AGENT_RUNTIME_WEB_SEARCH_PROVIDER` | `tavily` | Runtime web-search fallback provider: `none` or `tavily`; Tavily is only called for in-scope questions when KB/history does not provide a reliable answer |
| `AGENT_QUESTION_PLANNER_PROVIDER` | `rules` | `rules` or `llm`; planner receives the latest customer message plus compact greeting metadata and decides scope, history routing, and explicit human requests |
| `OPENAI_API_KEY` | unset | Required when `AGENT_ANSWER_PROVIDER=openai`, `AGENT_EMBEDDING_PROVIDER=openai`, or `AGENT_QUESTION_PLANNER_PROVIDER=llm` |
| `AGENT_LLM_MODEL` | `gpt-4.1-mini` | OpenAI chat model used by LLM-backed answer generation and planning |
| `AGENT_LLM_TEMPERATURE` | `0.0` | LLM sampling temperature |
| `AGENT_DATABASE_URL` | unset | PostgreSQL connection string |
| `AGENT_CONFIDENCE_THRESHOLD` | `0.60` | Below this, mark the response as low confidence |
| `AGENT_CONVERSATION_HISTORY_MAX_MESSAGES` | `50` | Safety cap for exact current-conversation messages passed into context |
| `AGENT_GREETING_LAPSE_MINUTES` | `60` | Minutes after the previous customer message before the prompt says to greet again |
| `AGENT_TENANT_CONFIG_CACHE_PROVIDER` | `memory` | Tenant config cache provider: `memory` or `redis`; Helm defaults to `redis` |
| `AGENT_TENANT_CONFIG_CACHE_TTL_SECONDS` | `300` | TTL for Redis tenant config cache entries |
| `AGENT_REDIS_URL` | unset | Redis URL required when `AGENT_TENANT_CONFIG_CACHE_PROVIDER=redis`; Helm points this at the bundled Redis service |
| `AGENT_VECTOR_COLLECTION` | `customer-service` | Default vector collection/index name used by tenant config defaults; tenant namespaces isolate data |
| `AGENT_TELEGRAM_CREDENTIAL_PROVIDER` | `kubernetes` | Telegram credential provider; only tenant-specific Kubernetes Secret lookup is currently supported |
| `AGENT_TELEGRAM_SECRET_NAMESPACE` | unset | Kubernetes namespace used for tenant Telegram Secret lookup; Helm defaults this to the pod namespace |
| `AGENT_TELEGRAM_BOT_TOKEN_SECRET_KEY` | `TELEGRAM_BOT_TOKEN` | Secret key containing a tenant Telegram bot token |
| `AGENT_TELEGRAM_WEBHOOK_SECRET_TOKEN_SECRET_KEY` | `TELEGRAM_WEBHOOK_SECRET_TOKEN` | Secret key containing a tenant Telegram webhook secret token |
| `AGENT_TELEGRAM_WEBHOOK_PUBLIC_BASE_URL` | unset | Public API base URL used by onboarding jobs to register Telegram webhooks; may include `/api` when the public web service proxies API traffic |
| `AGENT_WHATSAPP_SECRET_NAMESPACE` | unset | Kubernetes namespace used for tenant WhatsApp Secret lookup; Helm defaults this to the pod namespace |
| `AGENT_WHATSAPP_ACCESS_TOKEN_SECRET_KEY` | `WHATSAPP_ACCESS_TOKEN` | Secret key containing a tenant WhatsApp Cloud API access token |
| `AGENT_WHATSAPP_PHONE_NUMBER_ID_SECRET_KEY` | `WHATSAPP_PHONE_NUMBER_ID` | Secret key containing a tenant WhatsApp phone number id |
| `AGENT_WHATSAPP_VERIFY_TOKEN_SECRET_KEY` | `WHATSAPP_VERIFY_TOKEN` | Secret key containing a tenant WhatsApp webhook verify token |
| `AGENT_WHATSAPP_GRAPH_API_VERSION_SECRET_KEY` | `WHATSAPP_GRAPH_API_VERSION` | Optional Secret key containing a tenant WhatsApp Graph API version |
| `AGENT_WHATSAPP_GRAPH_API_VERSION` | `v20.0` | Default Meta Graph API version used when the tenant Secret does not provide one |
| `AGENT_LOG_LEVEL` | `INFO` | Application log level, for example `DEBUG` |
| `AGENT_LOG_FORMAT` | `{asctime} - {levelname}:{name}:{message}` | Python logging format using `{}` style |
| `LANGSMITH_TRACING` | `true` | Enable LangSmith tracing |
| `LANGSMITH_TRACING_V2` | `true` | Enable LangSmith tracing v2 |
| `LANGCHAIN_TRACING_V2` | `true` | Legacy LangChain tracing v2 env var kept for SDK compatibility |
| `LANGSMITH_ENDPOINT` | `https://eu.api.smith.langchain.com` | LangSmith endpoint |
| `LANGSMITH_PROJECT` | `customer-service-local` | Fallback LangSmith trace project used when tenant config does not provide `langsmith_project` |
| `LANGSMITH_WORKSPACE_ID` | unset | LangSmith workspace id; required for org-scoped API keys or keys linked to multiple workspaces |

The Helm chart also accepts these n8n values:

| Helm value | Default | Purpose |
|---|---|---|
| `n8n.enabled` | `false` | Optionally deploy local n8n for workflow experiments |
| `n8n.image` | `n8nio/n8n:2.33.7` | n8n container image |
| `n8n.port` | `5678` | Service and container port |
| `n8n.storage` | `1Gi` | PVC size for `/home/node/.n8n` |
| `n8n.timezone` | `Europe/Amsterdam` | Sets `GENERIC_TIMEZONE` and `TZ` |
| `n8n.webhookUrl` | unset | Public base URL for n8n webhooks when using a tunnel |
| `n8n.editorBaseUrl` | unset | Public editor URL when using a tunnel |
| `n8n.encryptionKey.existingSecret` | unset | Optional Kubernetes Secret containing `N8N_ENCRYPTION_KEY` |
| `n8n.encryptionKey.secretKey` | `N8N_ENCRYPTION_KEY` | Secret key used for the n8n encryption key |
| `n8n.owner.managedByEnv` | `false` | Pre-provision the n8n owner account from environment variables |
| `n8n.owner.email` | unset | Owner email when pre-provisioning is enabled |
| `n8n.owner.firstName` | unset | Owner first name when pre-provisioning is enabled |
| `n8n.owner.lastName` | unset | Owner last name when pre-provisioning is enabled |
| `n8n.owner.existingSecret` | unset | Kubernetes Secret containing the owner password bcrypt hash |
| `n8n.owner.passwordHashSecretKey` | `N8N_INSTANCE_OWNER_PASSWORD_HASH` | Secret key used for the owner password hash |
| `n8n.email.resendApiKeySecretName` | unset | Optional Kubernetes Secret containing `RESEND_API_KEY` for experimental workflow HTTP Request email calls |
| `n8n.email.resendApiKeySecretKey` | `RESEND_API_KEY` | Secret key used for the Resend API key |
| `n8n.email.onboardingEmail` | unset | Address exposed to n8n as `ONBOARDING_EMAIL` for experimental workflows |
| `n8n.code.allowBuiltinModules` | `url` | Built-in Node.js modules allowed in n8n Code nodes via `NODE_FUNCTION_ALLOW_BUILTIN` |
| `n8n.database.type` | `postgresdb` | n8n database type |
| `n8n.database.schema` | `public` | Postgres schema used by n8n |
| `n8n.database.tablePrefix` | `n8n_` | Prefix for n8n tables when sharing the app database |

Changing `AGENT_EMBEDDING_DIMENSIONS` changes the required pgvector column type. Use a
fresh database, recreate the `knowledge_documents` table, or reindex the KB when moving
between local 64-dimensional embeddings and OpenAI 1536-dimensional embeddings.
Knowledge rows use stable chunk ids such as `kb/menu.txt#0000` so callers can trace
answers to exact retrieved chunks.
API response citations return these chunk ids, not just source file paths, so callers can
trace an answer to the exact retrieved chunk.
Retrieved chunks passed to the LLM include `chunk_id`, `source`, and the pgvector
`created_at` timestamp for that chunk.
Onboarding-created KB chunks use source metadata including `source_url`, `source_type`,
`source_title`, `chunk_index`, `chunk_count`, `retrieved_at`, and `content_hash` when
available. URL-backed sources use stable chunk ids
like `url:<source-url-hash>#0000`; approved onboarding profile chunks use
`onboarding-profile:<tenant-id>#0000`.
The LLM prompt instructs the model to prefer newer `created_at` chunks only when multiple
relevant chunks overlap or conflict; retrieval ranking remains semantic-only.

## Increment 4 boundary

A `low_confidence: true` response indicates low answer confidence but does not persist a
handoff state. The graph can move a conversation to `HUMAN_REQUESTED` when the customer
explicitly asks for a human agent, but creating/reusing Telegram or WhatsApp support
groups, tracking internal agent discussion, deciding which agent messages to forward, and
forwarding those messages to the customer still belong to later increments.
