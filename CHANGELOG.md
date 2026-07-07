# Changelog

All notable changes to this project will be documented in this file.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project currently uses increment-based milestones instead of semantic versions.

## [Unreleased]

### Added

### Changed

### Fixed

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

- Startup KB loading now reads mounted files before falling back to demo documents.
- Startup KB documents are written and queried using the `seed-knowledge` namespace.
- README now documents the Rancher Desktop namespace and ConfigMap KB workflow.
- Helm NOTES now reference the app service name and namespace correctly.

### Fixed

- pgvector KB ingestion is idempotent by upserting on `(namespace, source)`.
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
  cites sources, and marks low-confidence replies for escalation.
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
