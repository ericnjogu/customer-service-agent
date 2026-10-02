"""Opt-in real model evaluation of the production post-response processor."""

import hashlib
import logging
import os

import pytest

from app.adapters.llm import create_openai_answer_generator, langsmith_client
from app.config import Settings
from app.issues import PROCESSOR_PROMPT, process_issue
from app.models import TenantConfig

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.skipif(
    os.getenv("AGENT_RUN_LIVE_OPENAI_TESTS", "").lower() != "true",
    reason="Set AGENT_RUN_LIVE_OPENAI_TESTS=true for billable model evaluations",
)


@pytest.fixture(autouse=True)
def live_tracing(monkeypatch):
    enabled = os.getenv("LANGSMITH_TRACING", "false").strip().lower() == "true"
    monkeypatch.setenv("LANGSMITH_TRACING", "true" if enabled else "false")
    if enabled and not os.getenv("LANGSMITH_API_KEY"):
        pytest.fail("LANGSMITH_TRACING=true requires LANGSMITH_API_KEY")
    project = os.getenv("LANGSMITH_PROJECT") or "issue-processor-live"
    monkeypatch.setattr(TENANT, "langsmith_project", project)
    logger.info("LangSmith tracing=%s project=%s", enabled, project if enabled else "disabled")
    try:
        yield
    finally:
        if enabled:
            # Keep upload draining in pytest's teardown timing, outside processor call timing.
            langsmith_client().flush(timeout=30)


@pytest.fixture
def generator():
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is required")
    settings = Settings()
    logger.info("Evaluating processor model=%s", settings.llm_model)
    logger.info(
        "Processor prompt source=%s sha256=%s",
        os.getenv("AGENT_ISSUE_PROCESSING_PROMPT_PATH") or "packaged:issue-processing.md",
        hashlib.sha256(PROCESSOR_PROMPT.encode()).hexdigest(),
    )
    return create_openai_answer_generator(
        api_key=settings.openai_api_key,
        model=settings.llm_model,
        temperature=0,
        base_url=settings.openai_base_url,
    )


def current_turn(request, response="Please provide more details.", delivery="accepted"):
    return [
        dict(event_id="request", sender_type="CUSTOMER", body=request, delivery="unknown"),
        dict(event_id="reply:request", sender_type="BOT", body=response, delivery=delivery),
    ]


TENANT = TenantConfig.with_defaults(
    "fictional-bakery",
    business_summary="Harbor Bakery sells bread and cakes. "
    "Allowed issue categories: ordering, billing, general-question.",
)


@pytest.mark.parametrize(
    "name,history,expected_type,terms",
    [
        ("greeting", current_turn("Hello", "Hello! How can I help?"), None, []),
        ("insufficient_context", current_turn("There is something I need help with"), None, []),
        ("unrelated", current_turn("Write a Python web scraper for me"), None, []),
        (
            "order_problem",
            current_turn("Cake order HP-1042 arrived damaged. Can you replace it?"),
            "ordering",
            ["HP-1042"],
        ),
        (
            "multiple_topics",
            current_turn(
                "Order HP-1042 was damaged and I was charged twice. "
                "Please replace the cake and refund the duplicate charge."
            ),
            "any",
            ["HP-1042", "charg"],
        ),
    ],
)
async def test_live_processor(generator, name, history, expected_type, terms):
    result = await process_issue(generator, TENANT, None, history)
    logger.info("case=%s result=%s", name, result.model_dump_json())
    if expected_type is None:
        assert result.issue is None
    else:
        assert result.issue is not None
        assert result.issue.issue_type in {"ordering", "billing", "question"}, (
            f"{name}: returned category {result.issue.issue_type!r} is outside the tenant's "
            f"closed vocabulary. Business summary: {TENANT.business_summary}"
        )
        if expected_type != "any":
            assert result.issue.issue_type == expected_type
        for term in terms:
            assert term.lower() in result.issue.summary.lower()
        if name == "multiple_topics":
            assert result.issue.labels


async def test_live_processor_updates_summary_preserving_failed_delivery(generator):
    existing = {
        "summary": "Customer requested replacement of damaged cake order HP-1042.",
        "issue_type": "ordering",
        "labels": [],
    }
    history = current_turn(
        "Actually, please refund HP-1042 rather than replace it.",
        "I have recorded your request for human support.",
        delivery="failed",
    )
    result = await process_issue(generator, TENANT, existing, history)
    logger.info("case=correction_failed_delivery result=%s", result.model_dump_json())
    assert result.issue
    assert "HP-1042" in result.issue.summary
    assert "refund" in result.issue.summary.lower()
    assert "customer received" not in result.issue.summary.lower()
    assert "team notified" not in result.issue.summary.lower()


async def test_live_processor_reuses_representative_labels(generator):
    existing = {
        "summary": "Customer reports a delayed order HP-1042 and wants a refund.",
        "issue_type": "ordering",
        "labels": ["delivery", "refund"],
    }
    result = await process_issue(
        generator,
        TENANT,
        existing,
        current_turn(
            "Yes, HP-1042 is still late. I still want my money back.",
            "I cannot confirm its status. I can record your support request.",
        ),
    )
    assert result.issue
    assert set(result.issue.labels) == {"delivery", "refund"}


async def test_live_processor_condenses_long_customer_message(generator):
    request = (
        "My cake order HP-1042 arrived damaged and I want a refund, not a replacement. "
        + "The box was crushed and the icing was smeared across the packaging. " * 15
        + "Please keep the refund request open until this is resolved."
    )
    assert len(request.split()) > 100
    result = await process_issue(
        generator, TENANT, None, current_turn(request, "A replacement has already been dispatched.")
    )
    assert result.issue
    assert 1 <= len(result.issue.summary.split()) <= 100
    assert "HP-1042" in result.issue.summary
    assert "refund" in result.issue.summary.lower()
    assert "dispatch" not in result.issue.summary.lower()


@pytest.mark.parametrize(
    "customer_message,preceding,expected",
    [
        ("Hello", None, "neutral"),
        (
            "I already gave it to you! This is frustrating.",
            "Please provide your order number.",
            "negative",
        ),
        ("Thanks, that is really helpful!", "Your request was recorded.", "positive"),
        ("My order number is HP-1042.", "Please provide your order number.", "neutral"),
    ],
)
async def test_live_customer_reaction_sentiment(generator, customer_message, preceding, expected):
    context = (
        None
        if preceding is None
        else {
            "event_id": "previous-bot",
            "body": preceding,
            "delivery": "accepted",
        }
    )
    result = await process_issue(
        generator,
        TENANT,
        None,
        current_turn(customer_message, "The customer is very angry."),
        preceding_bot=context,
    )
    assert result.sentiment == expected


async def test_live_processor_ignores_bot_speculation(generator):
    existing = {
        "summary": "Customer reports missing chapati.",
        "issue_type": "ordering",
        "labels": ["delivery"],
    }
    result = await process_issue(
        generator,
        TENANT,
        existing,
        current_turn(
            "Why has order HP-1042 not arrived?",
            "Amazon says go to Your Orders. Grubhub and Sukhadia may have a backorder.",
        ),
    )
    assert result.issue
    assert len(result.issue.labels) <= 5
    assert result.issue.issue_type.isalpha()
    assert all(label.isalpha() for label in result.issue.labels)
    assert "HP-1042" in result.issue.summary
    assert len(result.issue.summary.split()) <= 100
    assert not any(
        name in result.issue.summary.lower()
        for name in ["amazon", "grubhub", "sukhadia", "backorder"]
    )


async def test_live_answer_does_not_adopt_unrelated_bot_claims(generator):
    from uuid import uuid4

    from app.models import StoredMessage

    history = [
        StoredMessage(
            conversation_id=uuid4(),
            event_id="old-bot",
            sender_type="BOT",
            body="Amazon upgrades shipping. Use Your Orders. Your cake is backordered at Grubhub.",
        )
    ]
    result = await generator.generate(
        "Why has my cake order HP-1042 not arrived?", [], history, tenant_config=TENANT
    )
    assert not result.answer_found
    assert not any(
        term in result.answer.lower()
        for term in ["https://", "your orders", "amazon", "grubhub", "sukhadia"]
    )
