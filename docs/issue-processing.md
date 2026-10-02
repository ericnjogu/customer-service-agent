# Conversation issue processing

Issue processing is **enabled by default**, including Helm local/production defaults.
Set `AGENT_ISSUE_PROCESSING_ENABLED=false` (Helm `issueProcessing.enabled=false`) to
disable issue associations, filtered history, new jobs and the worker without deleting data.
Enabled mode requires PostgreSQL (`AGENT_RETRIEVAL_PROVIDER=pgvector`,
`AGENT_DATABASE_URL`) and `AGENT_ANSWER_PROVIDER=openai` with `OPENAI_API_KEY`.
The existing tenant model selection and embedding configuration are reused.
Only the new `AGENT_ISSUE_PROCESSING_ENABLED` and `AGENT_ISSUE_PROCESSING_PROMPT_PATH`
names are accepted. Retired issue-detection environment variables cause a startup error;
replace them before rollout. No environment aliases or old Helm keys are supported.

## Behavior

### Per-turn sentiment

The same background call returns a required top-level `sentiment` (`positive`, `neutral`,
or `negative`) independently of `issue`. It assesses each in-scope customer message,
including greetings without an identifiable issue. Out-of-scope turns are not assessed.
`conversation_turn_sentiments` stores one assessment per tenant/conversation/customer
event, plus the preceding bot event and assessment timestamp. It is saved atomically with
issue changes and the processed marker; retries and embedding retries do not duplicate it.
The preceding response is selected strictly before the customer event in persisted order,
never the current turn's newly generated response. It is sentiment context only, not
issue-summary evidence. Failed sends are not evidence of a delivered response; unknown
delivery remains uncertain. Association is temporal, not proof of causation.
Join the stored event references to `messages` to display customer text, preceding response,
and delivery status chronologically. A null preceding reference is a baseline assessment;
responses without later assessed customer messages are unassessed, not neutral.
There is no historical backfill, issue-level sentiment, or separate sentiment model call.

### Issue summaries

One open issue per tenant/conversation; messages have a nullable, tenant-safe issue FK.
A background processor creates nothing for greetings or insufficient context. Once a
customer need is identifiable, it creates/updates one summary, main type and subtopic
labels and links only the triggering request/response pair. No earlier unassigned
messages are back-linked. Model inputs are business summary, the open issue, and only
the customer messages from that exact turn, not conversation history or bot responses.
Summaries use only customer statements (including prior customer facts retained in the
open issue); business summary supplies classification context, not customer-event evidence.
Summaries are limited to 100 whitespace-separated words, validated server-side as well
as instructed in the prompt. Multiple topics are intentionally grouped.
Types and up to five distinct labels are alphabetic single words (80 characters maximum).
Existing labels are reused unless unrepresentative; at capacity labels are consolidated
or replaced, not appended. Tenant categories constrain meaning; compound categories are
mapped to single words. No legacy-classification normalization is required.
Closed issues are excluded, never automatically reopened.

Answering uses the existing bounded history limit, excluding closed-issue messages.
Greeting metadata still uses actual customer-message timing. Summaries do not replace
history. Out-of-scope turns do not update an issue. Stable event IDs accompany each
current-turn message. There is no historical backfill, management UI, resolution or SLA logic.

The answering LLM no longer proposes issues. Its prompt asks for clarification where
necessary. Low-confidence replies state the information limitation without appending
or proactively offering a human-support request. Explicit requests and contextual consent change the conversation to
`HUMAN_REQUESTED`; this does **not** contact, notify or assign a person.

## Worker and storage

[PgQueuer 1.4.0](https://janbjorge.github.io/pgqueuer/) owns queue tables, notifications,
database-enforced concurrency, heartbeats, crash recovery, retries and failed jobs.
The application starts an in-process async worker; no extra pod or Redis queue is used.
Only one `conversation_issue` job runs globally, including across application replicas.
This trades background throughput for simpler ordering; reply generation remains independent.

Reply persistence and enqueueing share one PostgreSQL transaction. Queue payloads contain
only tenant/conversation/request/reply references. Jobs initially have a 60-second recovery delay;
after the channel send attempt, the application advances the deadline and sends a PgQueuer
notification. A crash before the attempt is recovered with delivery unknown. For HTTP,
eligibility follows persistence, not proof that the client received the response.
Successors wait with a far-future eligibility timestamp (year 9999); completing the
predecessor's summary and embedding phases releases the next turn and emits a notification. A held
failure blocks later turns in that conversation without consuming their retry budgets.
Replay the predecessor to resume the chain. Other conversations can still run.
Legacy queued payloads derive the request reference from the exact `reply:` event prefix.

PgQueuer 1.4 has no public expedite method. The small adapter updates only the initial
queued deadline and emits the library's typed notification. Queue claiming and retries
are not reimplemented. Its recovery timeout is 60 seconds; retries/replay may wait until
that sweep if no notification wakes the worker. Four exponential retries (five total
executions) are allowed, starting at five seconds, capped at five minutes. Terminal jobs
are held rather than deleted.

Explicit message columns record queue reference, processing completion and delivery.
`issue_processed_at` records committed issue mutation; `issue_completed_at` records
completion of the embedding phase and allows the successor to run. Existing completed
jobs absent from PgQueuer are marked complete at startup; no issue content is backfilled.
These are durable processing/delivery metadata, not provisional issues.
`accepted` means channel-provider acceptance, not a read receipt; `failed` and `unknown`
are retained for operational auditing, not supplied as summary evidence. Bot delivery
outcomes and content are excluded from summaries. Embedding retries skip completed summary mutations.
Summary versions and processed sequence guard against duplicate/stale updates.

New schema is initialized under a PostgreSQL advisory lock at startup. PgQueuer's own
schema is installed only if absent; future library upgrades require reviewing/running
its schema upgrade. No compatibility migration exists for the discarded JSONB/custom
queue implementation, which was not deployed.

## Operations and replay

Monitor `issue_jobs_processed` (success/failure), `issue_job_duration`, queue age and
failed jobs. The `issue.process` span includes tenant ID, tenant slug when present,
conversation ID and queue job ID. Provider exception content is excluded from stored
failure messages. Model calls use the existing LangSmith configuration.

Read-only queue inspection:
```sql
SELECT id, status, attempts, created, execute_after
FROM pgqueuer WHERE entrypoint = 'conversation_issue' ORDER BY created;

SELECT tenant_id, conversation_id, status, issue_type, labels, version,
       embedding_version, updated_at
FROM conversation_issues ORDER BY updated_at DESC;
```

After fixing a failed job's cause, use PgQueuer's replay API, not hand-edited attempt
counters. Supply the exact reviewed job ID:
```python
import asyncio
import os
import asyncpg
from pgqueuer import AsyncpgDriver, Queries

async def replay(job_id):
    connection = await asyncpg.connect(os.environ["AGENT_DATABASE_URL"])
    try:
        queries = Queries(AsyncpgDriver(connection))
        failed = await queries.list_failed_jobs(limit=1000)
        assert any(job.id == job_id and job.entrypoint == "conversation_issue" for job in failed)
        await queries.requeue_jobs([job_id])
    finally:
        await connection.close()

asyncio.run(replay(123))  # Replace with the reviewed failed-job ID.
```

Disabling retains pending work; re-enabling resumes it. Back up issue/message and queue
tables with the application database. Future tenant deletion must remove queue references
and issue associations in addition to existing tenant data.

## Tests

Ordinary tests explicitly disable workers unless testing this feature. CI supplies a
disposable pgvector PostgreSQL service and runs the real queue integration tests.

With Rancher Desktop running:
```bash
nerdctl run -d --name css-issue-test-db -p 127.0.0.1:55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=issue_tests pgvector/pgvector:pg16
AGENT_ISSUE_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/issue_tests \
  uv run pytest tests/test_issues.py -q
nerdctl rm -f -v css-issue-test-db
```
The test database user needs CREATE DATABASE; each test creates and removes an isolated
database. Never point this suite at production.

Opt-in, billable processor evaluations use the packaged `issue-processing.md` prompt,
production model client and `AGENT_LLM_MODEL`. They require an exported OPENAI_API_KEY:
```bash
AGENT_RUN_LIVE_OPENAI_TESTS=true \
  uv run pytest tests/test_issues_live.py -q -s --log-cli-level=INFO
```
Only fictional scenario data/results and the model are logged. Live calls are skipped by
default; skipped cases do not establish prompt quality. No LLM judge is used.

### Compare with and without LangSmith

Live processor tests disable LangSmith by default, even if an API key or the legacy
`LANGCHAIN_TRACING_V2` flag is present. Run the same scenarios both ways:

```bash
AGENT_RUN_LIVE_OPENAI_TESTS=true LANGSMITH_TRACING=false \
  uv run pytest tests/test_issues_live.py -q -s --log-cli-level=INFO --durations=0

# Export LANGSMITH_API_KEY securely before running this command.
AGENT_RUN_LIVE_OPENAI_TESTS=true LANGSMITH_TRACING=true \
LANGSMITH_PROJECT=issue-processor-live \
  uv run pytest tests/test_issues_live.py -q -s --log-cli-level=INFO --durations=0
```

The existing client also accepts
`LANGSMITH_ENDPOINT` for your region and `LANGSMITH_WORKSPACE_ID` where required.
For EU workspaces export `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`.
Tracing captures processor prompt/context and validated output, not client credentials.
The live fixture overrides the fictional tenant's project so traces appear in
`LANGSMITH_PROJECT` (default `issue-processor-live`). Production retains tenant routing.
Enabled tests flush uploads during teardown; compare `call` timings for processor
latency, and include `teardown` for total tracing overhead. Repeat comparisons because
provider/network latency varies. Disabled runs create no LangSmith client or processor
trace, but the already-installed SDK remains a dependency.

This uses LangSmith's [conditional tracing context](https://docs.langchain.com/langsmith/trace-without-env-vars).

The processor validates JSON with Pydantic and retries malformed output; it does not
assume JSON mode guarantees the schema
([OpenAI structured-output guidance](https://developers.openai.com/api/docs/guides/structured-outputs)).

The prompt can be overridden with `AGENT_ISSUE_PROCESSING_PROMPT_PATH`.
Helm supports `prompts.issueProcessing`, and bundled production prompts include the
processor. The local deploy script supplies it alongside the other Markdown prompts.

## Tenant-restricted website fallback

The graph resolves the tenant's website contact points before answer routing. It enters
search only for an in-scope insufficient answer with a provider and at least one valid
configured HTTP(S) website. Missing or unavailable configuration fails closed: no Tavily
call. Social accounts and URLs mentioned in customer messages are not website authority.
Tavily receives the configured host allowlist; only those hosts and their subdomains
are accepted. The answer generator receives filtered source text, not Tavily's synthesized
answer. Only accepted sources are cached. Old runtime-search chunks outside the current
allowlist are excluded from answers without deleting them. Onboarding research is unchanged.
Prompts prohibit speculative business facts, invented tracking pages, and treating prior
bot claims as independently verified facts. Low-confidence fallbacks remain low confidence.
