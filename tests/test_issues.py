import asyncio
import json
import os
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from pgqueuer import AsyncpgDriver, Job, Queries
from pydantic import ValidationError

from app.adapters.embeddings import LocalHashEmbeddingProvider
from app.adapters.issues import PostgresIssueRepository
from app.adapters.llm import LlmAnswerGenerator, LlmQuestionPlanner
from app.adapters.memory import MemoryTenantConfigRepository, RuleBasedQuestionPlanner
from app.adapters.postgres import PostgresConversationRepository, PostgresDatabase
from app.config import Settings
from app.container import create_container
from app.issues import (
    ENTRYPOINT,
    PROCESSOR_PROMPT,
    IssueDetails,
    IssueService,
    ProcessingResult,
    process_issue,
)
from app.models import IncomingMessage, StoredMessage


@pytest.mark.parametrize("has_knowledge", [False, True])
async def test_full_graph_to_mocked_provider_and_background_embeddings(
    repository, monkeypatch, has_knowledge
):
    from langchain_core.documents import Document

    from app.adapters.llm import create_openai_answer_generator
    from app.adapters.memory import MemoryRetrievalStore
    from app.graph import build_service_graph, invoke_service_graph

    calls = []

    async def completion(**request):
        calls.append(request)
        result = (
            Model().result
            if "trackable customer need" in request["messages"][0]["content"]
            else {
                "answer": "Please describe the damage to your screen.",
                "answer_found": True,
                "grounded": True,
                "confidence": 0.9,
            }
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))]
        )

    monkeypatch.setattr("litellm.acompletion", completion)
    generator = create_openai_answer_generator(
        api_key="test-not-a-key",
        model="gpt-5-mini",
        temperature=0,
    )
    repo = repository
    tenant_configs = MemoryTenantConfigRepository()
    retrieval = MemoryRetrievalStore()
    if has_knowledge:
        tenant = await tenant_configs.get("tenant")
        await retrieval.upsert(
            [
                Document(
                    page_content="For a cracked screen, describe the damage to your screen.",
                    metadata={"source": "screen-support", "chunk_id": "screen-support#0"},
                )
            ],
            tenant.vector_namespace,
        )
    worker = IssueService(repo, LocalHashEmbeddingProvider(64), generator, tenant_configs, "local")
    graph = build_service_graph(
        PostgresConversationRepository(repo.database),
        tenant_configs,
        retrieval,
        generator,
        RuleBasedQuestionPlanner(),
        0.6,
        100,
        60,
        issues=worker,
    )
    message = IncomingMessage(
        event_id="one",
        tenant_id="tenant",
        external_chat_id="chat",
        external_user_id="user",
        text="My screen cracked",
    )
    reply = await invoke_service_graph(graph, message)
    if has_knowledge:
        assert "damage" in reply.answer
    else:
        assert reply.answer == "I could not find enough information to answer that question."
        assert reply.low_confidence
    answer_calls = int(has_knowledge)
    assert len(calls) == answer_calls  # Issue processing is outside the answer path.
    await worker.delivered(message, "accepted")
    await worker.process(await job_for(repo))
    assert len(calls) == answer_calls + 1
    evidence = json.loads(calls[-1]["messages"][1]["content"])["messages"]
    assert [m["event_id"] for m in evidence] == ["one"]
    issue = await repo.database.pool.fetchrow("SELECT * FROM conversation_issues")
    assert issue["embedding_version"] == issue["version"] == 1
    assert (
        await repo.database.pool.fetchval(
            "SELECT count(*) FROM messages WHERE issue_id IS NOT NULL"
        )
        == 2
    )


async def test_out_of_scope_turn_and_schema_reinitialization(repository):
    repo = repository
    _, _, _, ref = await turn(repo)
    await repo.database.pool.execute("UPDATE messages SET in_scope=false")
    model = Model()
    await service(repo, model).process(await job_for(repo))
    assert not model.calls
    assert (await repo.snapshot(ref))["processed"]
    await repo.initialize()
    assert await repo.database.pool.fetchval("SELECT count(*) FROM messages") == 2
    assert (
        await repo.database.pool.fetchval("SELECT count(*) FROM conversation_turn_sentiments") == 0
    )


@pytest.mark.parametrize("value", ["positive", "neutral", "negative"])
def test_turn_sentiment_values(value):
    assert ProcessingResult(issue=None, sentiment=value).sentiment == value


@pytest.mark.parametrize("value", [None, "mixed", "Positive", 0, ""])
def test_turn_sentiment_invalid(value):
    with pytest.raises(ValidationError):
        ProcessingResult(issue=None, sentiment=value)


def test_turn_sentiment_required():
    with pytest.raises(ValidationError, match="sentiment"):
        ProcessingResult(issue=None)


@pytest.mark.parametrize("delivery", ["accepted", "failed", "unknown"])
async def test_turn_sentiment_links_preceding_response_without_issue(repository, delivery):
    repo = repository
    _, _, conversation, first = await turn(repo, text="Hello")
    worker = service(repo, Model({"issue": None, "sentiment": "neutral"}))
    await worker.process(await job_for(repo))
    await repo.delivery("tenant", "one", delivery)
    # Insert an unrelated conversation to verify the boundary.
    await turn(repo, event="other", chat="other")
    _, _, _, second = await turn(repo, event="second", text="That was not helpful!")
    snapshot = await repo.snapshot(second)
    assert snapshot["preceding_bot"]["event_id"] == "reply:one"
    model = Model({"issue": None, "sentiment": "negative"})
    await service(repo, model).process(await job_for(repo, event="reply:second"))
    await service(repo, model).process(await job_for(repo, event="reply:second"))
    assert len(model.calls) == 1
    payload = json.loads(model.calls[0][1].content)
    assert payload["preceding_bot_response_for_sentiment_only"]["delivery"] == delivery
    assert [m["event_id"] for m in payload["messages"]] == ["second"]
    history = await repo.sentiment_history("tenant", conversation.id)
    assert [r["sentiment"] for r in history] == ["neutral", "negative"]
    assert history[0]["preceding_bot_event_id"] is None
    assert history[1]["preceding_bot_event_id"] == "reply:one"
    assert history[1]["preceding_bot_delivery"] == delivery
    assert await repo.sentiment_history("another-tenant", conversation.id) == []
    assert await repo.database.pool.fetchval("SELECT count(*) FROM conversation_issues") == 0
    await repo.initialize()
    assert len(await repo.sentiment_history("tenant", conversation.id)) == 2


async def test_sentiment_failure_rolls_back_issue_and_processed_marker(repository):
    repo = repository
    _, _, _, ref = await turn(repo)
    snapshot = await repo.snapshot(ref)
    details = IssueDetails(summary="Screen damaged.", issue_type="repair")
    with pytest.raises(asyncpg.CheckViolationError):
        await repo.complete(ref, snapshot, details, sentiment="invalid")
    assert await repo.database.pool.fetchval("SELECT count(*) FROM conversation_issues") == 0
    assert (
        await repo.database.pool.fetchval("SELECT count(*) FROM conversation_turn_sentiments") == 0
    )
    assert not (await repo.snapshot(ref))["processed"]
    await turn(repo, event="foreign", chat="foreign")
    snapshot["preceding_bot"] = {"event_id": "reply:foreign"}
    with pytest.raises(ValueError, match="Invalid sentiment message references"):
        await repo.complete(ref, snapshot, details, sentiment="neutral")
    assert await repo.database.pool.fetchval("SELECT count(*) FROM conversation_issues") == 0
    snapshot["preceding_bot"] = None
    await repo.complete(ref, snapshot, details, sentiment="negative")
    assert (await repo.snapshot(ref))["processed"]


async def test_sentiment_predecessor_is_bounded_by_customer_not_current_reply(repository):
    repo = repository
    _, _, conversation, first = await turn(repo)
    await service(repo, Model({"issue": None})).process(await job_for(repo))
    # Two customer messages arrive before a new bot response is persisted.
    for event in ["second", "third"]:
        await repo.save_message(
            StoredMessage(
                tenant_id="tenant",
                conversation_id=conversation.id,
                event_id=event,
                sender_type="CUSTOMER",
                body="Please help",
            )
        )
    for event in ["second", "third"]:
        reply = StoredMessage(
            tenant_id="tenant",
            conversation_id=conversation.id,
            event_id=f"reply:{event}",
            sender_type="BOT",
            body="Checking",
        )
        await repo.enqueue(reply, event)
        ref = dict(
            tenant_id="tenant", conversation_id=str(conversation.id), event_id=reply.event_id
        )
        snapshot = await repo.snapshot(ref)
        assert snapshot["preceding_bot"]["event_id"] == "reply:one"
        await repo.complete(ref, snapshot, None, sentiment="neutral")
        await repo.finish(ref)
    history = await repo.sentiment_history("tenant", conversation.id)
    assert [row["preceding_bot_event_id"] for row in history] == [None, "reply:one", "reply:one"]


async def test_stale_job_cannot_overwrite_newer_summary(repository):
    repo = repository
    _, _, _, ref = await turn(repo)
    older = await repo.snapshot(ref)
    _, _, _, new_ref = await turn(repo, event="two")
    with pytest.raises(ValueError, match="Earlier issue turn"):
        await repo.snapshot(new_ref)
    await repo.complete(ref, older, IssueDetails(summary="old summary", issue_type="repair"))
    await repo.finish(ref)
    latest = await repo.snapshot(new_ref)
    await repo.complete(new_ref, latest, IssueDetails(summary="new summary", issue_type="repair"))
    await repo.complete(ref, older, IssueDetails(summary="old summary", issue_type="repair"))
    assert (
        await repo.database.pool.fetchval("SELECT summary FROM conversation_issues")
        == "new summary"
    )


class Model:
    def __init__(self, result=None):
        self.result = result or {
            "issue": {
                "summary": "Screen cracked; refund requested.",
                "issue_type": "repair",
                "labels": ["refund"],
            }
        }
        self.calls = []
        self.result.setdefault("sentiment", "neutral")

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps(self.result))


@pytest.fixture
async def repository():
    url = os.getenv("AGENT_ISSUE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set AGENT_ISSUE_TEST_DATABASE_URL to a disposable pgvector database")
    name = "issue_test_" + uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE DATABASE "{name}"')
    db = PostgresDatabase(urlunsplit(urlsplit(url)._replace(path="/" + name)), 64)
    try:
        await db.initialize()
        repo = PostgresIssueRepository(db)
        await repo.initialize()
        yield repo
    finally:
        await db.close()
        await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()


async def turn(repo, event="one", chat="chat", tenant="tenant", text="My screen cracked"):
    message = IncomingMessage(
        tenant_id=tenant, event_id=event, external_chat_id=chat, external_user_id="user", text=text
    )
    conversation = await PostgresConversationRepository(repo.database).get_or_create(message)
    await repo.save_message(
        StoredMessage(
            tenant_id=tenant,
            conversation_id=conversation.id,
            event_id=event,
            sender_type="CUSTOMER",
            body=text,
        )
    )
    reply = StoredMessage(
        tenant_id=tenant,
        conversation_id=conversation.id,
        event_id=f"reply:{event}",
        sender_type="BOT",
        body="Please describe it.",
    )
    await repo.enqueue(reply, event)
    ref = dict(tenant_id=tenant, conversation_id=str(conversation.id), event_id=reply.event_id)
    return message, reply, conversation, ref


def service(repo, model=None, embeddings=None):
    return IssueService(
        repo,
        embeddings or LocalHashEmbeddingProvider(64),
        LlmAnswerGenerator(model or Model()),
        MemoryTenantConfigRepository(),
        "local",
    )


async def job_for(repo, event="reply:one"):
    row = await repo.database.pool.fetchrow(
        """SELECT q.* FROM pgqueuer q JOIN messages m
        ON m.issue_queue_id=q.id WHERE m.event_id=$1""",
        event,
    )
    return Job.model_validate(dict(row))


async def wait_until(predicate, timeout=5):
    async def wait():
        while not await predicate():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(wait(), timeout)


async def test_defaults_and_missing_postgres(monkeypatch):
    monkeypatch.delenv("AGENT_ISSUE_PROCESSING_ENABLED")
    assert Settings(_env_file=None).issue_processing_enabled
    with pytest.raises(ValueError, match="Issue processing requires PostgreSQL"):
        await create_container(Settings(retrieval_provider="memory", _env_file=None))
    container = await create_container(Settings(issue_processing_enabled=False, _env_file=None))
    assert container.issues is None
    await container.close()


@pytest.mark.parametrize("suffix", ["ENABLED", "PROMPT_PATH"])
def test_retired_environment_variables_fail_actionably(monkeypatch, suffix):
    monkeypatch.setenv(f"AGENT_ISSUE_DETECTION_{suffix}", "false")
    with pytest.raises(ValueError, match=f"replace it with AGENT_ISSUE_PROCESSING_{suffix}"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "labels",
    [
        ["one", "two", "three", "four", "five", "six"],
        ["order status"],
        ["order-status"],
        ["order_status"],
        ["HP1042"],
        ["refund", "Refund"],
        [""],
        ["x" * 81],
    ],
)
def test_invalid_issue_labels(labels):
    with pytest.raises(ValidationError):
        IssueDetails(summary="A request", issue_type="ordering", labels=labels)


@pytest.mark.parametrize("issue_type", ["general-question", "order problem", "order1", ""])
def test_invalid_issue_types(issue_type):
    with pytest.raises(ValidationError):
        IssueDetails(summary="A request", issue_type=issue_type)


def test_single_words_allow_unicode_and_five_distinct_labels():
    details = IssueDetails(
        summary="A request",
        issue_type="réclamation",
        labels=["delivery", "refund", "damage", "delay", "quality"],
    )
    assert len(details.labels) == 5


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"issue": {"summary": "x"}},
        {"issue": {"summary": "x", "issue_type": "repair", "labels": ["x"] * 9}},
        {"issue": None, "action": "reopen"},
    ],
)
def test_invalid_processor_result(payload):
    with pytest.raises(ValidationError):
        ProcessingResult.model_validate(payload)


async def test_processor_uses_packaged_prompt_and_provenance():
    model = Model({"issue": None})
    customer = {"event_id": "one", "sender_type": "CUSTOMER", "body": "Refund please"}
    messages = [
        customer,
        {
            "event_id": "reply:one",
            "sender_type": "BOT",
            "body": "Refund sent",
            "delivery": "failed",
        },
        {"event_id": "agent", "sender_type": "AGENT", "body": "Replacement sent"},
    ]
    result = await process_issue(LlmAnswerGenerator(model), None, None, messages)
    assert result.issue is None
    assert model.calls[0][0].content == PROCESSOR_PROMPT
    assert json.loads(model.calls[0][1].content)["messages"] == [customer]


@pytest.mark.parametrize("count", [1, 99, 100])
def test_summary_word_limit_accepts_boundary(count):
    summary = " \n\t".join(["word"] * count)
    assert IssueDetails(summary=summary, issue_type="ordering").summary == summary


@pytest.mark.parametrize("summary", ["word " * 101, " \n\t"])
def test_summary_word_limit_rejects_invalid_result(summary):
    with pytest.raises(ValidationError, match="between 1 and 100 words"):
        ProcessingResult.model_validate({"issue": {"summary": summary, "issue_type": "ordering"}})


async def test_processor_receives_tenant_categories_verbatim():
    from app.models import TenantConfig

    summary = "Bakery. Allowed issue categories: ordering, billing, general-question."
    tenant = TenantConfig.with_defaults("bakery", business_summary=summary)
    model = Model({"issue": {"summary": "Damaged order.", "issue_type": "ordering", "labels": []}})
    result = await process_issue(LlmAnswerGenerator(model), tenant, None, [])
    assert json.loads(model.calls[0][1].content)["business_summary"] == summary
    assert result.issue.issue_type == "ordering"


@pytest.mark.parametrize("enabled", [False, True])
async def test_processor_conditional_tracing(monkeypatch, enabled):
    from langsmith.run_helpers import get_tracing_context

    from app.models import TenantConfig

    monkeypatch.setenv("LANGSMITH_TRACING", str(enabled).lower())
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-secret-not-for-serialization")
    calls = []
    outputs = []
    client = object()

    def get_client():
        assert enabled, "Disabled tracing must not create a client"
        return client

    @contextmanager
    def record_trace(name, **kwargs):
        assert enabled
        assert get_tracing_context()["enabled"] is True
        calls.append((name, kwargs))
        yield SimpleNamespace(end=lambda **values: outputs.append(values))

    monkeypatch.setattr("app.issues.langsmith_client", get_client)
    monkeypatch.setattr("app.issues.langsmith_trace", record_trace)
    tenant = TenantConfig.with_defaults(
        "bakery", langsmith_project="test-issues", business_summary="A local bakery."
    )
    model = Model()
    history = [
        {"event_id": "one", "sender_type": "CUSTOMER", "body": "Refund please"},
        {"event_id": "two", "sender_type": "BOT", "body": "Which order?", "delivery": "failed"},
    ]
    issue = {"summary": "Customer requested a refund.", "issue_type": "billing"}
    result = await process_issue(LlmAnswerGenerator(model), tenant, issue, history)
    assert result.issue.summary == model.result["issue"]["summary"]
    assert len(model.calls) == 1
    assert len(calls) == int(enabled)
    if enabled:
        name, config = calls[0]
        assert name == "issue_processing"
        assert config["client"] is client
        assert config["project_name"] == "test-issues"
        assert config["metadata"]["tenant_id"] == "bakery"
        traced_messages = config["inputs"]["messages"]
        assert traced_messages == [
            {"role": message.role, "content": message.content} for message in model.calls[0]
        ]
        assert traced_messages[0] == {"role": "system", "content": PROCESSOR_PROMPT}
        assert traced_messages[1]["role"] == "user"
        assert json.loads(traced_messages[1]["content"]) == {
            "preceding_bot_response_for_sentiment_only": None,
            "business_summary": tenant.business_summary,
            "open_issue": issue,
            "messages": [history[0]],
        }
        assert "test-secret-not-for-serialization" not in str(config)
        assert outputs == [{"outputs": result.model_dump(mode="json")}]


async def test_delayed_processing_links_only_current_turn_and_one_issue(repository):
    repo = repository
    first, _, conversation, _ = await turn(repo, text="Hello")
    model = Model({"issue": None})
    worker = service(repo, model)
    await worker.process(await job_for(repo))
    assert not await repo.database.pool.fetchval("SELECT count(*) FROM conversation_issues")
    assert all(m.issue_id is None for m in await repo.history(conversation, 100))
    await turn(repo, event="two", text="Cracked screen and refund for the delayed delivery")
    model.result = Model().result
    await worker.process(await job_for(repo, "reply:two"))
    issue = await repo.database.pool.fetchrow("SELECT * FROM conversation_issues")
    assert issue["status"] == "open"
    assert issue["labels"] == ["refund"]
    assert issue["embedding_version"] == issue["version"] == 1
    history = await repo.history(conversation, 100)
    assert all(m.issue_id is None for m in history[:2])
    assert all(m.issue_id == issue["id"] for m in history[2:])
    payload = json.loads(model.calls[1][1].content)
    assert [m["event_id"] for m in payload["messages"]] == ["two"]
    await turn(repo, event="three", text="Actually replace it, and add a charger")
    assert (
        await repo.database.pool.fetchval(
            "SELECT count(*) FROM messages "
            "WHERE event_id IN ('three','reply:three') AND issue_id IS NULL"
        )
        == 2
    )
    await worker.process(await job_for(repo, "reply:three"))
    assert await repo.database.pool.fetchval("SELECT count(*) FROM conversation_issues") == 1
    assert await repo.database.pool.fetchval("SELECT version FROM conversation_issues") == 2
    await worker.process(await job_for(repo, "reply:three"))
    assert len(model.calls) == 3
    assert await repo.database.pool.fetchval("SELECT version FROM conversation_issues") == 2


async def test_atomic_enqueue_duplicate_and_rollback(repository, monkeypatch):
    repo = repository
    _, reply, _, _ = await turn(repo)
    await repo.enqueue(reply, "one")
    assert await repo.database.pool.fetchval("SELECT count(*) FROM pgqueuer") == 1

    async def fail(*args, **kwargs):
        raise RuntimeError("enqueue unavailable")

    monkeypatch.setattr(Queries, "enqueue", fail)
    with pytest.raises(RuntimeError):
        await repo.enqueue(reply.model_copy(update={"event_id": "reply:other"}), "other")
    assert not await repo.database.pool.fetchval(
        "SELECT 1 FROM messages WHERE event_id='reply:other'"
    )


async def test_snapshot_pairs_exact_turn_despite_interleaving_and_rejects_foreign_ref(repository):
    repo = repository
    _, _, conversation, ref = await turn(repo)
    await repo.save_message(
        StoredMessage(
            tenant_id="tenant",
            conversation_id=conversation.id,
            event_id="interleaved",
            sender_type="CUSTOMER",
            body="A different request, not this turn",
        )
    )
    snapshot = await repo.snapshot(ref)
    assert [m["event_id"] for m in snapshot["messages"]] == ["one", "reply:one"]
    await turn(repo, event="foreign", chat="other-chat")
    with pytest.raises(ValueError, match="cross-conversation"):
        await repo.snapshot({**ref, "customer_event_id": "foreign"})
    await repo.complete(ref, snapshot, IssueDetails(summary="Screen damaged", issue_type="repair"))
    assert await repo.database.pool.fetchval(
        "SELECT issue_id IS NULL FROM messages WHERE event_id='interleaved'"
    )


@pytest.mark.parametrize("suffix", ["ENABLED", "PROMPT_PATH"])
def test_retired_dotenv_variables_fail_actionably(tmp_path, monkeypatch, suffix):
    # pytest's temporary file is an isolated configuration fixture, not project config.
    env_file = tmp_path / ".env"
    env_file.write_text(f"AGENT_ISSUE_DETECTION_{suffix}=false\n")
    with pytest.raises(ValueError, match=f"replace it with AGENT_ISSUE_PROCESSING_{suffix}"):
        Settings(_env_file=env_file)


async def test_scope_foreign_key_closed_history_and_uninspected_messages(repository):
    repo = repository
    _, _, conversation, ref = await turn(repo)
    snapshot = await repo.snapshot(ref)
    await repo.complete(ref, snapshot, IssueDetails(summary="Cracked", issue_type="repair"))
    history = await repo.history(conversation, 10)
    assert history[0].issue_id == history[1].issue_id
    issue = history[1].issue_id
    _, _, other, other_ref = await turn(repo, event="other", tenant="other", chat="other")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await repo.database.pool.execute(
            "UPDATE messages SET issue_id=$1 WHERE tenant_id='other'", issue
        )
    assert (await repo.snapshot(other_ref))["issue"] is None
    await repo.database.pool.execute(
        "UPDATE conversation_issues SET status='resolved' WHERE id=$1", issue
    )
    assert len(await repo.history(conversation, 10)) == 0
    # Existing generic history remains unchanged when the feature is disabled.
    assert (
        len(
            await PostgresConversationRepository(repo.database).list_messages_since(
                conversation.id, conversation.created_at, 10
            )
        )
        == 2
    )


async def test_embedding_retry_blocks_successor_without_repeating_mutation(repository):
    repo = repository
    _, _, _, ref = await turn(repo)
    model = Model()

    class FailingEmbedding:
        async def embed_query(self, text):
            raise RuntimeError("temporary")

    worker = service(repo, model, FailingEmbedding())
    with pytest.raises(RuntimeError):
        await worker.process(await job_for(repo))
    _, _, _, next_ref = await turn(repo, event="next")
    with pytest.raises(ValueError, match="Earlier issue turn"):
        await repo.snapshot(next_ref)
    worker.embeddings = LocalHashEmbeddingProvider(64)
    await worker.process(await job_for(repo))
    assert len(model.calls) == 1
    assert (await repo.snapshot(next_ref))["messages"][0]["event_id"] == "next"
    stale = dict(await repo.database.pool.fetchrow("SELECT * FROM conversation_issues"))
    await repo.database.pool.execute("UPDATE conversation_issues SET version=version+1")
    assert await repo.publish(stale, [1.0] * 64, "old") == "UPDATE 0"


@pytest.mark.parametrize("outcome", ["accepted", "failed", "unknown"])
async def test_worker_wakes_after_delivery_and_recovers_startup(repository, outcome):
    repo = repository
    message, _, _, _ = await turn(repo)
    worker = service(repo)
    await worker.start()
    try:
        await asyncio.sleep(0.1)
        assert not await repo.database.pool.fetchval("SELECT count(*) FROM conversation_issues")
        await worker.delivered(message, outcome)
        await wait_until(
            lambda: repo.database.pool.fetchval("SELECT count(*) FROM conversation_issues")
        )
        assert (
            await repo.database.pool.fetchval(
                "SELECT delivery FROM messages WHERE event_id='reply:one'"
            )
            == outcome
        )
    finally:
        await worker.close()
    # Simulate a crash before sending: delayed recovery processes unknown delivery.
    await turn(repo, event="two")
    await repo.database.pool.execute(
        "UPDATE pgqueuer SET execute_after=now() WHERE status='queued'"
    )
    worker = service(repo)
    await worker.start()
    try:
        await wait_until(
            lambda: repo.database.pool.fetchval(
                "SELECT issue_processed_at IS NOT NULL FROM messages WHERE event_id='reply:two'"
            )
        )
    finally:
        await worker.close()


async def test_two_workers_global_limit_and_expired_heartbeat(repository):
    repo = repository
    await turn(repo)
    await turn(repo, event="two", chat="other")
    # Simulate a job picked by a dead process.
    await repo.database.pool.execute("""UPDATE pgqueuer SET status='picked',
        heartbeat=now()-interval '5 minutes',updated=now()-interval '5 minutes',slot=0
        WHERE id=(SELECT min(id) FROM pgqueuer)""")
    await repo.database.pool.execute("UPDATE pgqueuer SET execute_after=now()")
    active, peak = 0, 0

    class SlowModel(Model):
        async def ainvoke(self, *args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.1)
            try:
                return await super().ainvoke(*args, **kwargs)
            finally:
                active -= 1

    workers = [service(repo, SlowModel()) for _ in range(2)]
    try:
        for worker in workers:
            await worker.start()
        await wait_until(lambda: _count_is(repo, 2))
        assert peak == 1
    finally:
        for worker in workers:
            await worker.close()


async def _count_is(repo, count):
    return await repo.database.pool.fetchval("SELECT count(*) FROM conversation_issues") == count


async def test_five_attempts_held_failure_and_replay(repository):
    repo = repository
    message, _, _, _ = await turn(repo)
    later, _, _, later_ref = await turn(repo, event="later")
    model = Model()
    model.result = {"invalid": True}
    worker = service(repo, model)
    executor = worker.manager.entrypoint_registry[ENTRYPOINT]
    executor.initial_delay = timedelta(milliseconds=1)
    executor.max_delay = timedelta(milliseconds=5)
    await worker.delivered(message, "accepted")
    await worker.delivered(later, "accepted")
    await worker.start(recovery_interval=timedelta(milliseconds=20))
    try:
        await wait_until(
            lambda: repo.database.pool.fetchval(
                "SELECT count(*) FROM pgqueuer WHERE status='failed'"
            )
        )
        assert len(model.calls) == 5
        assert (await job_for(repo, "reply:later")).attempts == 0
        with pytest.raises(ValueError, match="Earlier issue turn"):
            await repo.snapshot(later_ref)
        job = await job_for(repo)
        assert job.attempts == 4
        model.result = Model().result
        async with repo.database.pool.acquire() as c:
            await Queries(AsyncpgDriver(c)).requeue_jobs([job.id])
        await wait_until(
            lambda: repo.database.pool.fetchval(
                "SELECT issue_processed_at IS NOT NULL FROM messages WHERE event_id='reply:later'"
            )
        )
        assert len(model.calls) == 7
        assert await _count_is(repo, 1)
    finally:
        await worker.close()


async def test_contextual_consent_and_decline():
    history = [
        StoredMessage(
            conversation_id=uuid4(),
            event_id="offer",
            sender_type="BOT",
            body="Would you like me to record a request for human support?",
        )
    ]
    message = IncomingMessage(
        event_id="yes", external_chat_id="c", external_user_id="u", text="yes"
    )
    planner = RuleBasedQuestionPlanner()
    assert (await planner.plan(message, conversation_history=history)).explicit_human_request
    assert not (await planner.plan(message)).explicit_human_request
    assert not (
        await planner.plan(message.model_copy(update={"text": "no"}), conversation_history=history)
    ).explicit_human_request
    model = Model({"in_scope": True, "explicit_human_request": True})
    await LlmQuestionPlanner(model).plan(message, conversation_history=history)
    assert "record a request" in model.calls[0][1].content
