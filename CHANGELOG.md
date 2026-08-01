# Changelog

All notable changes to this project will be documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project currently uses increment-based milestones instead of semantic versions.

## [Unreleased]

### Added

- Startup logging for seed knowledge loading, mounted KB file discovery, and pgvector
  document upserts, with KB file/upsert traces visible at `INFO`.
- `AGENT_LOG_LEVEL` / `logging.level` configuration for controlling application logs.
- `AGENT_LOG_FORMAT` / `logging.format` configuration using Python logging `{}` style.
- Logging filter that suppresses noisy Uvicorn access logs for `/healthz` probes.
- Local deploy helper script for Rancher Desktop Kubernetes rebuilds and rollouts.
- Conversation state API for reading conversations and updating routing state.
- State event persistence in PostgreSQL for conversation routing changes.
- Customer-message API endpoint at `POST /messages/customer`.
- Optional OpenAI/LangChain answer provider behind `AGENT_ANSWER_PROVIDER=openai`.
- Helm values and environment variables for model, temperature, and OpenAI API key secret.
- Local run/deploy scripts can accept `OPENAI_API_KEY` and enable the OpenAI answer provider.
- Helm values for LangSmith tracing environment variables, including Secret-backed API key
  support.
- Exact current-conversation history context, bounded by conversation start time and a
  configurable message safety cap.
- Derived greeting metadata for LLM prompts, using a configurable 60-minute default lapse
  without exposing absolute timestamps.
- Telegram customer webhook adapter for text messages, with optional webhook secret
  validation and outbound `sendMessage` replies.
- WhatsApp Cloud API customer webhook adapter for text messages, with Meta webhook
  verification and outbound text replies.
- Question planner boundary with local rule-based and OpenAI/LLM-backed implementations
  for deciding whether the latest message is in scope, whether conversation history is
  needed, and whether the customer explicitly requested a human agent.
- `AGENT_QUESTION_PLANNER_PROVIDER` / `questionPlanner.provider` configuration for
  choosing `rules` or `llm`.
- Embedding provider boundary with local hash embeddings and OpenAI semantic embeddings.
- `AGENT_EMBEDDING_PROVIDER`, `AGENT_EMBEDDING_MODEL`, and
  `AGENT_EMBEDDING_DIMENSIONS` configuration.
- Helm values for OpenAI `text-embedding-3-small` embeddings with 1536 dimensions.
- Knowledge chunking helper via LangChain `RecursiveCharacterTextSplitter`.
- Tenant id foundation for inbound messages, conversation records, replies, tenant-scoped
  conversation lookup, tenant-scoped message idempotency, and tenant-specific seed KB
  namespaces.
- `AGENT_DEFAULT_TENANT_ID` / `tenant.defaultId` configuration for local/default tenant
  routing.
- Tenant prompt configuration storage and API endpoints for per-tenant answer and planner
  prompt instruction overlays.
- Dedicated tenant API router and tenant config request validation for supported plans,
  features, LLM providers, vector providers, isolation modes, and LLM base URLs.
- Tenant opt-in feature flags, plus tenant control-plane fields for LLM project
  metadata, LangSmith project names, tenant LLM provider metadata, and generic vector
  provider/collection/namespace settings.
- Tenant selected plan reference (`sme` or `enterprise`) for onboarding/audit context,
  with plan templates in Helm values instead of runtime inference.
- Redis-backed tenant config read-through cache with memory fallback for local/non-Redis
  runs.
- Tenant channel credential references for one Telegram Secret and one WhatsApp Secret per
  tenant, without storing secret values in Postgres.
- Tenant-specific Telegram credential resolution from Kubernetes Secrets, including
  tenant webhook secret validation and tenant bot-token selection for outbound replies.
- Bruno onboarding steps for creating tenant Telegram Kubernetes Secrets and registering
  Telegram webhooks, plus Telegram utility requests for inspecting and deleting a bot
  webhook.
- `AGENT_VECTOR_COLLECTION` / `vector.collection` configuration for the shared vector
  collection or index default used by tenant records.
- Documentation clarifying that `llm_project_id` and `llm_project_name` are stable,
  provider-neutral tenant metadata fields that can map to a workspace, account scope,
  billing project, or equivalent grouping for non-OpenAI LLM providers.
- Basic RAG and human handover are treated as inbuilt capabilities, not opt-in tenant
  features.
- Tenant records with generated immutable `tnt_...` tenant ids, readable unique slugs,
  and `POST /tenants` / `GET /tenants/{tenant_id}` API endpoints. Tenant config is
  created separately with `PUT /tenants/{tenant_id}/config`.
- Tenant creation returns `409 Conflict` when the derived/explicit slug already exists,
  while `GET /tenants/by-slug/{slug}` provides deliberate existing-tenant lookup for
  onboarding flows.
- Bruno tenant config creation uses the tenant slug for provider-facing project names and
  vector namespaces (`customer-service-<slug>` and `<slug>:seed-knowledge`).
- Bruno onboarding requests for creating a tenant, creating provider projects, capturing
  runtime variables from responses, and creating the tenant config from those values.
- LangSmith tracing now targets the deployment-level `LANGSMITH_PROJECT`; tenant
  LangSmith/project fields are retained as tags/metadata for filtering and audit.
- Message scope persistence marks out-of-scope customer messages and their bot replies so
  they can be retained for audit/debugging without being loaded into future LLM
  conversation-history context.

### Changed

- Collapsed explicit human-request detection into the question planner, removing the
  separate human-request detector graph node and configuration.
- Renamed the application configuration environment prefix from `SUPPORT_` to `AGENT_`,
  including the tenant header from `X-Support-Tenant-Id` to `X-Agent-Tenant-Id`.
- Removed global Telegram bot Secret/env fallback wiring; Telegram replies now use
  tenant-scoped Kubernetes Secrets referenced by `telegram_secret_name`.
- Removed global WhatsApp Cloud API Secret/env wiring; WhatsApp webhook verification and
  replies now use tenant-scoped Kubernetes Secrets referenced by `whatsapp_secret_name`.
- Removed the over-scoped PDF upload ingestion path, ingestion worker, S3/MinIO object
  store wiring, and scanned-PDF OCR dependencies so future online-source or
  cloud-document connectors can be added behind a smaller, deliberate boundary.
- Removed ConfigMap/startup seed KB loading from the app and Helm chart; KB data should
  now be populated through retrieval storage, future source tools, cloud-document
  connectors, learned support answers, or admin workflows.
- Collapsed conversation status handling into one routing state: `BOT_ACTIVE`,
  `HUMAN_REQUESTED`, and `HUMAN_ACTIVE`.
- Low-confidence graph replies now return `low_confidence: true` without changing the
  persisted conversation state.
- Explicit human-agent requests now move conversations from `BOT_ACTIVE` to
  `HUMAN_REQUESTED` while the bot continues answering with retrieved context.
- Answer generation now uses a configurable provider boundary; `extractive` remains the
  default local provider.
- LLM answer prompts now include current conversation history alongside retrieved KB
  documents.
- LLM answer prompts now include compact greeting metadata so the model can avoid repeated
  greetings during active conversations.
- LLM answer prompts now instruct the model to prefer newer relevant KB chunks when
  retrieved chunks overlap or conflict.
- The support graph now plans each question before retrieval/answering, skips KB retrieval
  for out-of-scope questions, uses the planner explanation as the customer-facing reply,
  and skips conversation history for standalone in-scope questions.
- The question planner prompt now receives compact greeting metadata from the previous
  customer-message time delta so out-of-scope replies can avoid repeated greetings during
  active conversations.
- Conversation-history prompt entries now include message `created_at` timestamps, and
  answer prompts instruct the LLM not to use greeting metadata as evidence for exact
  message times.
- Conversation-history prompt blocks now describe their line format, chronological order,
  sender types, and how to identify the first customer message.
- Conversation-history prompt entries now include a readable relative age alongside the
  exact `created_at` timestamp.
- `httpx` is now an application dependency because Telegram reply delivery uses outbound
  HTTP calls at runtime.
- Removed hard-coded demo seed documents; startup KB seeding now only loads explicitly
  configured files.
- pgvector KB upserts now use `content_hash` to skip unchanged documents before computing
  embeddings.
- pgvector KB storage now stores one vector row per chunk using stable chunk ids instead
  of one vector row per source file.
- Service reply citations now return chunk ids when available, falling back to source only
  for documents without chunk metadata.
- Retrieved KB chunks passed to the LLM now include chunk id, source, and pgvector
  creation timestamp metadata.

### Fixed

- pgvector search now accepts JSONB metadata returned by the driver as either a mapping or
  JSON string, preventing customer-message retrieval crashes.
- Local MVP token embeddings now normalize simple plural variants such as `socials` and
  `handles`, improving deterministic retrieval for small KB documents.

### Deferred

## [Iteration 2] - 2026-07-07

### Added

- File-based knowledge-base loading from `.md` and `.txt` files.
- Helm support for mounting an existing ConfigMap as the startup KB directory.
- `AGENT_KNOWLEDGE_PATH` configuration for external KB directories.
- `knowledge` Helm values for ConfigMap name, mount path, and startup seeding.
- Shared `SEED_KNOWLEDGE_NAMESPACE` constant set to `seed-knowledge`.
- Regression test for loading KB documents from a directory.

### Changed

- Startup KB loading reads mounted files when a knowledge path is configured.
- Startup KB documents are written and queried using the `seed-knowledge` namespace.
- README now documents the Rancher Desktop namespace and ConfigMap KB workflow.
- Helm NOTES now reference the app service name and namespace correctly.

### Fixed

- pgvector KB ingestion is idempotent by upserting on `(namespace, chunk_id)`.
- Existing duplicate KB rows are deduplicated during schema initialization before the
  unique index is created.
- KB rows now track `created_at` and `updated_at`, with `updated_at` refreshed when a
  file is changed and reloaded.

### Deferred

- Admin KB upload UI and document approval workflow.
- Automatic removal or archival of KB rows when files disappear from the mounted directory.
- Human support group handover.
- Production LLM and embedding providers.

## [Iteration 1] - 2026-07-03

### Added

- FastAPI service with a synthetic webhook endpoint at `POST /webhooks/synthetic`.
- LangGraph support workflow that persists incoming messages, retrieves context, answers,
  cites sources, and flags low-confidence replies.
- In-memory conversation and retrieval adapters for local development and tests.
- PostgreSQL-backed conversation persistence.
- pgvector-backed retrieval store with local deterministic embeddings.
- Helm chart for local Kubernetes deployment with PostgreSQL and pgvector.
- Init container that waits for PostgreSQL to become available before starting the app.
- `uv`-based Python project setup.
- API and graph tests for the initial vertical slice.

### Deferred

- Telegram and WhatsApp webhook adapters.
- Durable conversation memory retrieval.
- Service issue state transitions.
- Human handover and support group forwarding.
