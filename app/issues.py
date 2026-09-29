"""Post-response issue processing; PgQueuer owns job execution and recovery."""

import asyncio
import json
import logging
import time
from datetime import timedelta
from functools import partial
from typing import Annotated

from langsmith import trace as langsmith_trace
from langsmith import tracing_context
from opentelemetry import metrics, trace
from pgqueuer import AsyncpgPoolDriver, DatabaseRetryEntrypointExecutor, Queries, QueueManager
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from app.adapters.llm import (
    ChatPromptMessage,
    langsmith_client,
    langsmith_tracing_enabled,
    load_prompt,
    tenant_langsmith_project_name,
    tenant_trace_metadata,
)

logger = logging.getLogger(__name__)
meter = metrics.get_meter(__name__)
counter = meter.create_counter("issue_jobs_processed")
duration = meter.create_histogram("issue_job_duration", unit="s")
PROCESSOR_PROMPT = load_prompt("issue-processing.md", "AGENT_ISSUE_PROCESSING_PROMPT_PATH")
ENTRYPOINT = "conversation_issue"


def alphabetic_word(value: str) -> str:
    if not value.isalpha():
        raise ValueError("Must be a single alphabetic word")
    return value


IssueWord = Annotated[
    str, StringConstraints(min_length=1, max_length=80), AfterValidator(alphabetic_word)
]


class IssueDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=4000)
    issue_type: IssueWord
    labels: list[IssueWord] = Field(default_factory=list, max_length=5)

    @field_validator("summary")
    @classmethod
    def summary_word_limit(cls, summary):
        if not 1 <= len(summary.split()) <= 100:
            raise ValueError("Summary must contain between 1 and 100 words")
        return summary

    @field_validator("labels")
    @classmethod
    def distinct_labels(cls, labels):
        if len({label.casefold() for label in labels}) != len(labels):
            raise ValueError("Labels must be distinct")
        return labels


class ProcessingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue: IssueDetails | None


async def process_issue(generator, tenant, issue, messages) -> ProcessingResult:
    prompt_messages = [
        ChatPromptMessage(role="system", content=PROCESSOR_PROMPT),
        ChatPromptMessage(
            role="user",
            content=json.dumps(
                {
                    "business_summary": tenant.business_summary if tenant else None,
                    "open_issue": issue,
                    "messages": [
                        message for message in messages if message.get("sender_type") == "CUSTOMER"
                    ],
                },
                default=str,
            ),
        ),
    ]
    enabled = langsmith_tracing_enabled()
    with tracing_context(enabled=enabled):
        if not enabled:
            return await _process_issue(generator, tenant, prompt_messages)
        # Explicit inputs avoid serializing the generator and its provider credentials.
        with langsmith_trace(
            "issue_processing",
            client=langsmith_client(),
            project_name=tenant_langsmith_project_name(tenant),
            metadata=tenant_trace_metadata(tenant),
            inputs={
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in prompt_messages
                ],
            },
        ) as run:
            result = await _process_issue(generator, tenant, prompt_messages)
            run.end(outputs=result.model_dump(mode="json"))
            return result


async def _process_issue(generator, tenant, prompt_messages) -> ProcessingResult:
    response = await asyncio.wait_for(
        generator.chat_model_for(tenant).ainvoke(prompt_messages),
        timeout=90,
    )
    return ProcessingResult.model_validate_json(str(response.content))


class IssueService:
    def __init__(self, repository, embeddings, generator, tenant_configs, model):
        self.repository = repository
        self.embeddings = embeddings
        self.generator = generator
        self.tenant_configs = tenant_configs
        self.model = model
        self.driver = AsyncpgPoolDriver(repository.database.pool)
        self.manager = QueueManager(Queries(self.driver))
        self.manager.entrypoint(
            ENTRYPOINT,
            concurrency_limit=1,
            on_failure="hold",
            # PgQueuer counts retries from zero: four retries = five executions.
            executor_factory=partial(
                DatabaseRetryEntrypointExecutor, max_attempts=4, initial_delay=timedelta(seconds=5)
            ),
        )(self.process)
        self.task = None

    async def start(self, *, recovery_interval=timedelta(seconds=60)):
        await self.manager.verify_structure()
        self.task = asyncio.create_task(
            self.manager.run(
                batch_size=1,
                max_concurrent_tasks=2,
                dequeue_timeout=recovery_interval,
            ),
            name="issue-pgqueuer",
        )

    async def close(self):
        self.manager.shutdown.set()
        try:
            if self.task:
                try:
                    await asyncio.wait_for(self.task, timeout=10)
                except asyncio.TimeoutError:
                    logger.warning("Issue worker stopped; PgQueuer will recover unfinished work")
        finally:
            await self.driver.__aexit__(None, None, None)

    async def record(self, reply, message):
        await self.repository.enqueue(reply, message.event_id)

    async def delivered(self, message, outcome):
        try:
            await self.repository.delivery(message.tenant_id, message.event_id, outcome)
        except Exception:
            logger.warning("Issue delivery update failed; deferred job will recover as unknown")

    async def process(self, job):
        started = time.monotonic()
        ref = json.loads(job.payload)
        with trace.get_tracer(__name__).start_as_current_span("issue.process") as span:
            span.set_attribute("tenant.id", ref["tenant_id"])
            span.set_attribute("conversation.id", ref["conversation_id"])
            span.set_attribute("issue.job_id", str(job.id))
            try:
                snapshot = await self.repository.snapshot(ref)
                if snapshot["finished"]:
                    return
                tenant = await self.tenant_configs.get(ref["tenant_id"])
                slug = await self.repository.database.pool.fetchval(
                    "SELECT slug FROM tenants WHERE tenant_id=$1", ref["tenant_id"]
                )
                if slug:
                    span.set_attribute("tenant.slug", slug)
                if not snapshot["processed"]:
                    decision = (
                        await process_issue(
                            self.generator, tenant, snapshot["issue"], snapshot["messages"]
                        )
                        if snapshot["messages"]
                        else ProcessingResult(issue=None)
                    )
                    await self.repository.complete(ref, snapshot, decision.issue)
                target = await self.repository.embedding_target(ref)
                if target:
                    vector = await asyncio.wait_for(
                        self.embeddings.embed_query(target["summary"]),
                        timeout=30,
                    )
                    await self.repository.publish(target, vector, self.model)
                await self.repository.finish(ref)
                counter.add(1, {"outcome": "success"})
            except Exception as error:
                counter.add(1, {"outcome": "failure"})
                # Do not let PgQueuer persist provider payloads/credentials in tracebacks.
                raise RuntimeError(f"Issue processing failed ({type(error).__name__})") from None
            finally:
                duration.record(time.monotonic() - started)
