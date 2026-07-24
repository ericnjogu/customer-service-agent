from datetime import datetime, timedelta, timezone

from langchain_core.documents import Document

from app.adapters.memory import RuleBasedHumanRequestDetector, RuleBasedQuestionPlanner
from app.config import Settings
from app.container import create_container
from app.graph import build_prompt_metadata, build_support_graph, invoke_support_graph
from app.knowledge import SEED_KNOWLEDGE_NAMESPACE
from app.models import ConversationPromptMetadata, IncomingMessage, QuestionPlan, StoredMessage


class RecordingAnswerGenerator:
    def __init__(self) -> None:
        self.histories: list[list[StoredMessage]] = []
        self.metadata: list[ConversationPromptMetadata | None] = []
        self.calls = 0

    async def generate(
        self,
        query: str,
        documents,
        conversation_history: list[StoredMessage] | None = None,
        conversation_metadata: ConversationPromptMetadata | None = None,
    ) -> tuple[str, float]:
        self.calls += 1
        self.histories.append(conversation_history or [])
        self.metadata.append(conversation_metadata)
        return "Recorded", 0.95


class StaticQuestionPlanner:
    def __init__(self, plan: QuestionPlan) -> None:
        self.plan_value = plan
        self.calls = 0
        self.metadata: list[ConversationPromptMetadata | None] = []

    async def plan(
        self,
        message: IncomingMessage,
        conversation_metadata: ConversationPromptMetadata | None = None,
    ) -> QuestionPlan:
        self.calls += 1
        self.metadata.append(conversation_metadata)
        return self.plan_value


async def test_grounded_question_returns_citation(tmp_path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "password-reset.txt").write_text(
        "To reset your password, open Settings, select Security, then choose Reset password.",
        encoding="utf-8",
    )

    container = await create_container(Settings(knowledge_path=str(knowledge_dir)))
    reply = await invoke_support_graph(
        container.graph,
        IncomingMessage(
            event_id="event-1",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="How do I reset my password?",
        ),
    )

    assert reply.low_confidence is False
    assert reply.citations == ["kb/password-reset.txt#0000"]
    assert "Settings" in reply.answer
    assert reply.state == "BOT_ACTIVE"


async def test_unknown_question_is_marked_low_confidence() -> None:
    container = await create_container(Settings())
    reply = await invoke_support_graph(
        container.graph,
        IncomingMessage(
            event_id="event-2",
            external_chat_id="chat-2",
            external_user_id="user-2",
            text="Can you explain quantum chromodynamics?",
        ),
    )

    assert reply.low_confidence is True
    assert reply.confidence == 0.0
    assert reply.citations == []
    assert reply.state == "BOT_ACTIVE"

    conversation = await container.conversations.get_by_id(reply.conversation_id)
    assert conversation.state == "BOT_ACTIVE"


async def test_loads_knowledge_from_directory(tmp_path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "shipping.md").write_text(
        "Shipping address changes are allowed before the order is packed.",
        encoding="utf-8",
    )

    container = await create_container(Settings(knowledge_path=str(knowledge_dir)))
    reply = await invoke_support_graph(
        container.graph,
        IncomingMessage(
            event_id="event-3",
            external_chat_id="chat-3",
            external_user_id="user-3",
            text="Can I change my shipping address?",
        ),
    )

    assert reply.low_confidence is False
    assert reply.citations == ["kb/shipping.md#0000"]
    assert "Shipping address changes" in reply.answer
    assert reply.state == "BOT_ACTIVE"


async def test_seed_knowledge_namespace_constant_is_used(tmp_path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "shipping.md").write_text(
        "Shipping address changes are allowed before the order is packed.",
        encoding="utf-8",
    )

    container = await create_container(Settings(knowledge_path=str(knowledge_dir)))

    assert SEED_KNOWLEDGE_NAMESPACE == "seed-knowledge"
    assert SEED_KNOWLEDGE_NAMESPACE in container.retrieval.documents
    assert "knowledge" not in container.retrieval.documents


async def test_graph_passes_current_conversation_history_with_safety_cap() -> None:
    container = await create_container(Settings(seed_knowledge=False))
    generator = RecordingAnswerGenerator()
    graph = build_support_graph(
        container.conversations,
        container.retrieval,
        generator,
        RuleBasedQuestionPlanner(),
        RuleBasedHumanRequestDetector(),
        confidence_threshold=0.60,
        conversation_history_max_messages=2,
        greeting_lapse_minutes=60,
    )

    for index in range(4):
        await invoke_support_graph(
            graph,
            IncomingMessage(
                event_id=f"history-event-{index}",
                external_chat_id="history-chat",
                external_user_id="history-user",
                text=f"Can I still get that answer {index}?",
            ),
        )

    final_history = generator.histories[-1]
    assert [message.body for message in final_history] == [
        "Recorded",
        "Can I still get that answer 3?",
    ]


async def test_graph_uses_question_plan_explanation_for_out_of_scope_reply() -> None:
    container = await create_container(Settings(seed_knowledge=False))
    generator = RecordingAnswerGenerator()
    planner = StaticQuestionPlanner(
        QuestionPlan(
            in_scope=False,
            needs_conversation_history=False,
            explanation="Hi Ada, I can help with questions about Maxys Lounge.",
        )
    )
    graph = build_support_graph(
        container.conversations,
        container.retrieval,
        generator,
        planner,
        RuleBasedHumanRequestDetector(),
        confidence_threshold=0.60,
        conversation_history_max_messages=2,
        greeting_lapse_minutes=60,
    )

    reply = await invoke_support_graph(
        graph,
        IncomingMessage(
            event_id="out-of-scope-event",
            external_chat_id="out-of-scope-chat",
            external_user_id="out-of-scope-user",
            sender_name="Ada Lovelace",
            text="1+1?",
        ),
    )

    assert planner.calls == 1
    assert generator.calls == 0
    assert reply.answer == "Hi Ada, I can help with questions about Maxys Lounge."
    assert reply.confidence == 0
    assert reply.low_confidence is True
    assert reply.citations == []


async def test_graph_passes_no_greet_metadata_to_planner_for_active_conversation() -> None:
    container = await create_container(Settings(seed_knowledge=False))
    generator = RecordingAnswerGenerator()
    planner = StaticQuestionPlanner(
        QuestionPlan(
            in_scope=False,
            needs_conversation_history=False,
            explanation="I can help with questions about Maxys Lounge.",
        )
    )
    graph = build_support_graph(
        container.conversations,
        container.retrieval,
        generator,
        planner,
        RuleBasedHumanRequestDetector(),
        confidence_threshold=0.60,
        conversation_history_max_messages=2,
        greeting_lapse_minutes=60,
    )

    await invoke_support_graph(
        graph,
        IncomingMessage(
            event_id="active-out-of-scope-1",
            external_chat_id="active-out-of-scope-chat",
            external_user_id="active-out-of-scope-user",
            sender_name="Ada Lovelace",
            text="1+1?",
        ),
    )
    reply = await invoke_support_graph(
        graph,
        IncomingMessage(
            event_id="active-out-of-scope-2",
            external_chat_id="active-out-of-scope-chat",
            external_user_id="active-out-of-scope-user",
            sender_name="Ada Lovelace",
            text="2+2?",
        ),
    )

    assert planner.metadata[-1] is not None
    assert planner.metadata[-1].should_greet_customer is False
    assert planner.metadata[-1].greeting_reason == "active conversation; avoid repeated greeting"
    assert reply.answer == "I can help with questions about Maxys Lounge."


async def test_graph_skips_conversation_history_for_standalone_question() -> None:
    container = await create_container(Settings(seed_knowledge=False))
    await container.retrieval.upsert(
        [
            Document(
                page_content="Maxys Lounge is located on River Road.",
                metadata={"source": "kb/location.txt", "chunk_id": "kb/location.txt#0000"},
            )
        ],
        SEED_KNOWLEDGE_NAMESPACE,
    )
    generator = RecordingAnswerGenerator()
    planner = StaticQuestionPlanner(
        QuestionPlan(
            in_scope=True,
            needs_conversation_history=False,
            explanation="standalone location question",
        )
    )
    graph = build_support_graph(
        container.conversations,
        container.retrieval,
        generator,
        planner,
        RuleBasedHumanRequestDetector(),
        confidence_threshold=0.60,
        conversation_history_max_messages=10,
        greeting_lapse_minutes=60,
    )

    await invoke_support_graph(
        graph,
        IncomingMessage(
            event_id="previous-history-event",
            external_chat_id="standalone-chat",
            external_user_id="standalone-user",
            text="Can I still get that?",
        ),
    )
    await invoke_support_graph(
        graph,
        IncomingMessage(
            event_id="standalone-location-event",
            external_chat_id="standalone-chat",
            external_user_id="standalone-user",
            text="Where are you located?",
        ),
    )

    assert generator.histories[-1] == []
    assert generator.metadata[-1] is not None
    assert generator.metadata[-1].should_greet_customer is False
    assert (
        generator.metadata[-1].greeting_reason
        == "active conversation; avoid repeated greeting"
    )


async def test_explicit_human_request_sets_human_requested_state() -> None:
    container = await create_container(Settings(seed_knowledge=False))
    await container.retrieval.upsert(
        [
            Document(
                page_content="Refund requests can be submitted within 30 days of purchase.",
                metadata={"source": "kb/refunds.txt", "chunk_id": "kb/refunds.txt#0000"},
            )
        ],
        SEED_KNOWLEDGE_NAMESPACE,
    )
    reply = await invoke_support_graph(
        container.graph,
        IncomingMessage(
            event_id="human-request-event",
            external_chat_id="human-request-chat",
            external_user_id="human-request-user",
            text="Can I speak to a human agent about refund requests?",
        ),
    )

    assert reply.state == "HUMAN_REQUESTED"
    assert reply.low_confidence is False
    assert "Refund requests" in reply.answer
    assert reply.citations == ["kb/refunds.txt#0000"]

    conversation = await container.conversations.get_by_id(reply.conversation_id)
    assert conversation.state == "HUMAN_REQUESTED"


async def test_frustration_without_human_request_does_not_change_state() -> None:
    container = await create_container(Settings(seed_knowledge=False))
    reply = await invoke_support_graph(
        container.graph,
        IncomingMessage(
            event_id="frustration-event",
            external_chat_id="frustration-chat",
            external_user_id="frustration-user",
            text="This answer is not helpful.",
        ),
    )

    assert reply.state == "BOT_ACTIVE"
    assert reply.low_confidence is True


async def test_citations_fall_back_to_source_without_chunk_id() -> None:
    container = await create_container(Settings(seed_knowledge=False))
    await container.retrieval.upsert(
        [
            Document(
                page_content="Shipping address changes are allowed before packing.",
                metadata={"source": "kb/shipping.md"},
            )
        ],
        SEED_KNOWLEDGE_NAMESPACE,
    )

    reply = await invoke_support_graph(
        container.graph,
        IncomingMessage(
            event_id="source-fallback-event",
            external_chat_id="source-fallback-chat",
            external_user_id="source-fallback-user",
            text="Can I change my shipping address?",
        ),
    )

    assert reply.citations == ["kb/shipping.md"]


def test_prompt_metadata_greets_first_customer_message() -> None:
    metadata = build_prompt_metadata(
        IncomingMessage(
            event_id="first",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Hello",
        ),
        [],
        greeting_lapse_minutes=60,
    )

    assert metadata.is_first_customer_message is True
    assert metadata.customer_name is None
    assert metadata.minutes_since_last_customer_message is None
    assert metadata.should_greet_customer is True
    assert metadata.greeting_reason == "first customer message in this conversation"


def test_prompt_metadata_uses_first_sender_name() -> None:
    metadata = build_prompt_metadata(
        IncomingMessage(
            event_id="first",
            external_chat_id="chat-1",
            external_user_id="user-1",
            sender_name="Ada Lovelace",
            text="Hello",
        ),
        [],
        greeting_lapse_minutes=60,
    )

    assert metadata.customer_name == "Ada"


def test_prompt_metadata_avoids_repeated_greeting_during_active_conversation() -> None:
    now = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
    metadata = build_prompt_metadata(
        IncomingMessage(
            event_id="current",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="One more question",
            received_at=now,
        ),
        [
            StoredMessage(
                conversation_id="00000000-0000-0000-0000-000000000000",
                event_id="previous",
                sender_type="CUSTOMER",
                body="Hello",
                created_at=now - timedelta(minutes=59),
            )
        ],
        greeting_lapse_minutes=60,
    )

    assert metadata.is_first_customer_message is False
    assert metadata.minutes_since_last_customer_message == 59
    assert metadata.should_greet_customer is False
    assert metadata.greeting_reason == "active conversation; avoid repeated greeting"


def test_prompt_metadata_greets_after_configured_lapse() -> None:
    now = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
    metadata = build_prompt_metadata(
        IncomingMessage(
            event_id="current",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Are you there?",
            received_at=now,
        ),
        [
            StoredMessage(
                conversation_id="00000000-0000-0000-0000-000000000000",
                event_id="previous",
                sender_type="CUSTOMER",
                body="Hello",
                created_at=now - timedelta(minutes=60),
            )
        ],
        greeting_lapse_minutes=60,
    )

    assert metadata.is_first_customer_message is False
    assert metadata.minutes_since_last_customer_message == 60
    assert metadata.should_greet_customer is True
    assert metadata.greeting_reason == "last customer message was 60 minutes ago"
