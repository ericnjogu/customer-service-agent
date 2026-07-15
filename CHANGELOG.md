# Changelog

All notable changes to this project will be documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project currently uses increment-based milestones instead of semantic versions.

## [Unreleased]

### Added

- Startup logging for seed knowledge loading, mounted KB file discovery, and pgvector
  document upserts, with KB file/upsert traces visible at `INFO`.
- `SUPPORT_LOG_LEVEL` / `logging.level` configuration for controlling application logs.
- `SUPPORT_LOG_FORMAT` / `logging.format` configuration using Python logging `{}` style.
- Logging filter that suppresses noisy Uvicorn access logs for `/healthz` probes.
- Local deploy helper script for Rancher Desktop Kubernetes rebuilds and rollouts.
- Conversation state API for reading conversations and updating routing state.
- State event persistence in PostgreSQL for conversation routing changes.
- Customer-message API endpoint at `POST /messages/customer`.
- Optional OpenAI/LangChain answer provider behind `SUPPORT_ANSWER_PROVIDER=openai`.
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
- Human-request detector boundary with local rule-based and OpenAI/LLM-backed
  implementations.
- `SUPPORT_HUMAN_REQUEST_DETECTOR_PROVIDER` / `humanRequestDetector.provider`
  configuration for choosing `rules` or `llm`.
- Embedding provider boundary with local hash embeddings and OpenAI semantic embeddings.
- `SUPPORT_EMBEDDING_PROVIDER`, `SUPPORT_EMBEDDING_MODEL`, and
  `SUPPORT_EMBEDDING_DIMENSIONS` configuration.
- Helm values for OpenAI `text-embedding-3-small` embeddings with 1536 dimensions.
- Seed KB chunking via LangChain `RecursiveCharacterTextSplitter`, configured with
  `SUPPORT_KNOWLEDGE_CHUNK_SIZE` and `SUPPORT_KNOWLEDGE_CHUNK_OVERLAP`.

### Changed

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
- `httpx` is now an application dependency because Telegram reply delivery uses outbound
  HTTP calls at runtime.
- Removed hard-coded demo seed documents; startup KB seeding now only loads explicitly
  configured files.
- pgvector KB upserts now use `content_hash` to skip unchanged documents before computing
  embeddings.
- pgvector KB storage now stores one vector row per chunk using stable chunk ids instead
  of one vector row per source file.
- Support reply citations now return chunk ids when available, falling back to source only
  for documents without chunk metadata.

### Fixed

- Seed knowledge loading now ignores Kubernetes ConfigMap projected-volume backing paths
  such as `..2026_*`, preventing duplicate file ingestion.
- pgvector search now accepts JSONB metadata returned by the driver as either a mapping or
  JSON string, preventing customer-message retrieval crashes.
- Local MVP token embeddings now normalize simple plural variants such as `socials` and
  `handles`, improving deterministic retrieval for small KB documents.

### Deferred

## [Iteration 2] - 2026-07-07

### Added

- File-based knowledge-base loading from `.md` and `.txt` files.
- Helm support for mounting an existing ConfigMap as the startup KB directory.
- `SUPPORT_KNOWLEDGE_PATH` configuration for external KB directories.
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
- Support issue state transitions.
- Human handover and support group forwarding.
