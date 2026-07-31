from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.adapters.llm import (
    LlmAnswerGenerator,
    LlmHumanRequestDetector,
    LlmQuestionPlanner,
    format_age,
    langsmith_client,
    langsmith_runnable_config,
    langsmith_tracing_enabled,
    tenant_trace_metadata,
    tenant_trace_tags,
)
from app.config import Settings
from app.container import create_container
from app.models import ConversationPromptMetadata, IncomingMessage, StoredMessage, TenantConfig


class FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0
        self.last_messages = None
        self.last_config = None

    async def ainvoke(self, messages, config=None):
        self.calls += 1
        self.last_messages = messages
        self.last_config = config
        return AIMessage(content=self.content)


def test_format_age_uses_readable_units() -> None:
    now = datetime(2026, 7, 25, 13, 34, tzinfo=timezone.utc)

    assert format_age(now - timedelta(minutes=1), now) == "1 minute ago"
    assert format_age(now - timedelta(minutes=26), now) == "26 minutes ago"
    assert format_age(now - timedelta(hours=2), now) == "2 hours ago"
    assert format_age(now - timedelta(days=2), now) == "2 days ago"


def test_tenant_trace_metadata_includes_provider_project_context() -> None:
    tenant_config = TenantConfig.with_defaults(
        "tenant-a",
        selected_plan="enterprise",
        enabled_features=["telegram", "whatsapp"],
        llm_project_id="proj_tenant_a",
        llm_provider="langchain-compatible",
        llm_model="deepseek-chat",
        llm_base_url="https://api.deepseek.com",
        vector_collection="customer-service",
        telegram_secret_name="tenant-a-telegram",
        whatsapp_secret_name="tenant-a-whatsapp",
    )

    assert tenant_trace_metadata(tenant_config) == {
        "tenant_id": "tenant-a",
        "selected_plan": "enterprise",
        "enabled_features": "telegram,whatsapp",
        "llm_provider": "langchain-compatible",
        "llm_model": "deepseek-chat",
        "llm_base_url": "https://api.deepseek.com",
        "vector_provider": "pgvector",
        "vector_isolation_mode": "shared_collection",
        "vector_collection": "customer-service",
        "vector_namespace": "tenant-a:seed-knowledge",
        "langsmith_project": "customer-service-tenant-a",
        "llm_project_name": "customer-service-tenant-a",
        "llm_project_id": "proj_tenant_a",
        "telegram_secret_name": "tenant-a-telegram",
        "whatsapp_secret_name": "tenant-a-whatsapp",
    }
    assert tenant_trace_tags(tenant_config) == [
        "tenant:tenant-a",
        "tenant-plan:enterprise",
    ]


def test_langsmith_tracing_enabled_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    assert langsmith_tracing_enabled() is False

    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")

    assert langsmith_tracing_enabled() is True


def test_langsmith_tracing_enabled_honors_disabled_env(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    assert langsmith_tracing_enabled() is False


def test_langsmith_client_uses_endpoint_and_workspace(monkeypatch) -> None:
    captured = {}

    class FakeLangSmithClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("app.adapters.llm.LangSmithClient", FakeLangSmithClient)
    langsmith_client.cache_clear()
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://eu.api.smith.langchain.com")
    monkeypatch.setenv("LANGSMITH_WORKSPACE_ID", "workspace-id")

    client = langsmith_client()

    assert client is not None
    assert captured == {
        "api_key": "test-key",
        "api_url": "https://eu.api.smith.langchain.com",
        "workspace_id": "workspace-id",
    }

    langsmith_client.cache_clear()


def test_langsmith_runnable_config_uses_env_project_destination(monkeypatch) -> None:
    captured_tracer = {}

    class FakeLangSmithClient:
        def __init__(self, **kwargs) -> None:
            pass

    class FakeLangChainTracer:
        def __init__(self, **kwargs) -> None:
            captured_tracer.update(kwargs)

    monkeypatch.setattr("app.adapters.llm.LangSmithClient", FakeLangSmithClient)
    monkeypatch.setattr("app.adapters.llm.LangChainTracer", FakeLangChainTracer)
    langsmith_client.cache_clear()
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_PROJECT", "customer-service-local")

    tenant_config = TenantConfig.with_defaults(
        "tenant-a",
        langsmith_project="customer-service-tenant-a",
    )

    config = langsmith_runnable_config("answer_generation", tenant_config)

    assert config is not None
    assert "project_name" not in captured_tracer
    assert "tenant:tenant-a" in config["tags"]
    assert config["metadata"]["langsmith_project"] == "customer-service-tenant-a"

    langsmith_client.cache_clear()


async def test_llm_answer_generator_returns_grounded_confidence() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Refunds are available within 30 days.", "confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    answer, confidence = await generator.generate(
        "What is the refund policy?",
        [Document(page_content="Refunds are available within 30 days.")],
    )

    assert answer == "Refunds are available within 30 days."
    assert confidence == 0.82
    assert chat_model.calls == 1


async def test_llm_answer_generator_instructs_model_to_prefer_newer_conflicting_chunks() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Refunds are available within 30 days.", "confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    await generator.generate(
        "What is the refund policy?",
        [Document(page_content="Refunds are available within 30 days.")],
    )

    system_prompt = chat_model.last_messages[0].content
    assert "prefer the chunk with the newer created_at timestamp" in system_prompt
    assert "it must still be relevant" in system_prompt


async def test_llm_answer_generator_instructs_model_to_reject_out_of_scope_questions() -> None:
    chat_model = FakeChatModel(
        '{"answer": "I am here to help with questions about this business.", '
        '"confidence": 0, "grounded": false}'
    )
    generator = LlmAnswerGenerator(chat_model)

    answer, confidence = await generator.generate(
        "Tell me a riddle.",
        [Document(page_content="Hustle HQ serves food and drinks.")],
    )

    system_prompt = chat_model.last_messages[0].content
    assert "Do not answer general-purpose questions" in system_prompt
    assert "math problems" in system_prompt
    assert "out-of-scope questions" in system_prompt
    assert answer == "I am here to help with questions about this business."
    assert confidence == 0


async def test_llm_answer_generator_instructs_model_to_reply_in_customer_language() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Tunaomba ufike kabla ya saa mbili usiku.", '
        '"confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    await generator.generate(
        "Mnafunga saa ngapi?",
        [Document(page_content="Hustle HQ closes at 8 PM.")],
    )

    system_prompt = chat_model.last_messages[0].content
    prompt = chat_model.last_messages[1].content
    assert "Use only the latest customer question to choose the response language" in (
        system_prompt
    )
    assert "Do not infer the" in system_prompt
    assert "response language from conversation history" in system_prompt
    assert "translate or summarize the grounded answer" in system_prompt
    assert "Response language instruction" in prompt
    assert "Use the language of the latest customer question below" in prompt
    assert "Ignore the language of conversation history" in prompt
    assert "Latest customer question:\nMnafunga saa ngapi?" in prompt


async def test_llm_answer_generator_includes_tenant_answer_instructions() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Karibu to Tenant A.", "confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    await generator.generate(
        "What do you serve?",
        [Document(page_content="Tenant A serves tea.")],
        tenant_config=TenantConfig(
            tenant_id="tenant-a",
            answer_prompt_instructions="Use Tenant A's warm brand voice.",
        ),
    )

    system_prompt = chat_model.last_messages[0].content
    prompt = chat_model.last_messages[1].content
    assert "Tenant-specific answer instructions may customize" in system_prompt
    assert "Tenant-specific answer instructions" in prompt
    assert "Use Tenant A's warm brand voice." in prompt


async def test_llm_answer_generator_caps_ungrounded_confidence() -> None:
    chat_model = FakeChatModel(
        '{"answer": "I do not have enough information.", "confidence": 0.9, "grounded": false}'
    )
    generator = LlmAnswerGenerator(chat_model)

    answer, confidence = await generator.generate(
        "What is my order status?",
        [Document(page_content="Refunds are available within 30 days.")],
    )

    assert answer == "I do not have enough information."
    assert confidence == 0.3


async def test_llm_answer_generator_includes_conversation_history() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Refunds are available within 30 days.", "confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    await generator.generate(
        "Can I still get one?",
        [Document(page_content="Refunds are available within 30 days.")],
        conversation_history=[
            StoredMessage(
                conversation_id="00000000-0000-0000-0000-000000000000",
                event_id="history-1",
                sender_type="CUSTOMER",
                body="I need a refund.",
                created_at=datetime(2026, 7, 25, 13, 8, tzinfo=timezone.utc),
            )
        ],
    )

    prompt = chat_model.last_messages[1].content
    assert "Conversation history for the current issue" in prompt
    assert "Format: one message per line as" in prompt
    assert "<created_at ISO-8601 timestamp> (<age relative to newest message>)" in prompt
    assert "<sender_type>: <message body>" in prompt
    assert "Order: oldest message first, newest message last." in prompt
    assert "CUSTOMER is the customer" in prompt
    assert "BOT is this assistant" in prompt
    assert "Use created_at for exact message times" in prompt
    assert "Use the relative age only as a readable summary" in prompt
    assert "Use the first CUSTOMER entry" in prompt
    assert "Messages:" in prompt
    assert "2026-07-25T13:08:00+00:00 (0 minutes ago) CUSTOMER: I need a refund." in prompt


async def test_llm_answer_generator_allows_history_only_answers() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Your first message was sent at 2026-07-25T13:08:00+00:00.", '
        '"confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    answer, confidence = await generator.generate(
        "When did I first send you a message?",
        [],
        conversation_history=[
            StoredMessage(
                conversation_id="00000000-0000-0000-0000-000000000000",
                event_id="history-1",
                sender_type="CUSTOMER",
                body="4*8?",
                created_at=datetime(2026, 7, 25, 13, 8, tzinfo=timezone.utc),
            ),
            StoredMessage(
                conversation_id="00000000-0000-0000-0000-000000000000",
                event_id="history-2",
                sender_type="CUSTOMER",
                body="When did I first send you a message?",
                created_at=datetime(2026, 7, 25, 13, 34, tzinfo=timezone.utc),
            )
        ],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name=None,
            minutes_since_last_customer_message=26,
            should_greet_customer=False,
            greeting_reason="active conversation; avoid repeated greeting",
        ),
    )

    system_prompt = chat_model.last_messages[0].content
    prompt = chat_model.last_messages[1].content
    assert answer == "Your first message was sent at 2026-07-25T13:08:00+00:00."
    assert confidence == 0.82
    assert chat_model.calls == 1
    assert "answer" in system_prompt
    assert "only from those conversation-history entries" in system_prompt
    assert "Do not infer exact times from conversation" in system_prompt
    assert "minutes_since_last_customer_message: 26" in prompt
    assert "2026-07-25T13:08:00+00:00 (26 minutes ago) CUSTOMER: 4*8?" in prompt
    assert (
        "2026-07-25T13:34:00+00:00 (0 minutes ago) CUSTOMER: "
        "When did I first send you a message?"
    ) in prompt


async def test_llm_answer_generator_includes_chunk_metadata() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Refunds are available within 30 days.", "confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    await generator.generate(
        "What is the refund policy?",
        [
            Document(
                page_content="Refunds are available within 30 days.",
                metadata={
                    "source": "kb/refunds.txt",
                    "chunk_id": "kb/refunds.txt#0000",
                    "created_at": "2026-07-16T10:00:00+00:00",
                },
            )
        ],
    )

    prompt = chat_model.last_messages[1].content
    assert "Knowledge base context" in prompt
    assert "chunk_id=kb/refunds.txt#0000" in prompt
    assert "source=kb/refunds.txt" in prompt
    assert "created_at=2026-07-16T10:00:00+00:00" in prompt


async def test_llm_answer_generator_includes_greeting_metadata_without_timestamps() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Refunds are available within 30 days.", "confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    await generator.generate(
        "Can I still get one?",
        [Document(page_content="Refunds are available within 30 days.")],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name="Ada",
            minutes_since_last_customer_message=60,
            should_greet_customer=True,
            greeting_reason="last customer message was 60 minutes ago",
        ),
    )

    prompt = chat_model.last_messages[1].content
    assert "Conversation metadata" in prompt
    assert "is_first_customer_message: false" in prompt
    assert "customer_name: Ada" in prompt
    assert "minutes_since_last_customer_message: 60" in prompt
    assert "should_greet_customer: true" in prompt
    assert "greeting_reason: last customer message was 60 minutes ago" in prompt
    assert "current_time" not in prompt
    assert "last_customer_message_at" not in prompt


async def test_llm_answer_generator_instructs_model_to_use_customer_name() -> None:
    chat_model = FakeChatModel(
        '{"answer": "Hi Ada, refunds are available within 30 days.", '
        '"confidence": 0.82, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    await generator.generate(
        "What is the refund policy?",
        [Document(page_content="Refunds are available within 30 days.")],
        conversation_metadata=ConversationPromptMetadata(
            is_first_customer_message=True,
            customer_name="Ada",
            should_greet_customer=True,
            greeting_reason="first customer message in this conversation",
        ),
    )

    system_prompt = chat_model.last_messages[0].content
    prompt = chat_model.last_messages[1].content
    assert "If customer_name is provided" in system_prompt
    assert "Do not invent a customer name" in system_prompt
    assert "customer_name: Ada" in prompt


async def test_llm_answer_generator_does_not_call_model_without_documents() -> None:
    chat_model = FakeChatModel('{"answer": "unused", "confidence": 1, "grounded": true}')
    generator = LlmAnswerGenerator(chat_model)

    answer, confidence = await generator.generate("Unknown?", [])

    assert answer == "I could not find enough information to answer that question."
    assert confidence == 0.0
    assert chat_model.calls == 0


async def test_llm_human_request_detector_detects_explicit_request() -> None:
    chat_model = FakeChatModel('{"explicit_human_request": true}')
    detector = LlmHumanRequestDetector(chat_model)

    detected = await detector.detect(
        IncomingMessage(
            event_id="detect-1",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Please connect me to a human agent.",
        )
    )

    assert detected is True
    assert chat_model.calls == 1
    assert "explicitly asks to speak with a human support person" in (
        chat_model.last_messages[0].content
    )


async def test_llm_human_request_detector_defaults_false_for_invalid_json() -> None:
    chat_model = FakeChatModel("not-json")
    detector = LlmHumanRequestDetector(chat_model)

    detected = await detector.detect(
        IncomingMessage(
            event_id="detect-2",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="This is not helpful.",
        )
    )

    assert detected is False


async def test_llm_question_planner_returns_structured_plan() -> None:
    chat_model = FakeChatModel(
        '{"in_scope": true, "needs_conversation_history": false, '
        '"explanation": "standalone location question"}'
    )
    planner = LlmQuestionPlanner(chat_model)

    plan = await planner.plan(
        IncomingMessage(
            event_id="plan-1",
            external_chat_id="chat-1",
            external_user_id="user-1",
            sender_name="Ada Lovelace",
            text="Where are you located?",
        ),
        ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name="Ada",
            minutes_since_last_customer_message=3,
            should_greet_customer=False,
            greeting_reason="active conversation; avoid repeated greeting",
        ),
    )

    assert plan.in_scope is True
    assert plan.needs_conversation_history is False
    assert plan.explanation == "standalone location question"
    assert "Decide using only the latest customer message" in (
        chat_model.last_messages[0].content
    )
    assert "explanation is the" in chat_model.last_messages[0].content
    assert "exact response the customer" in chat_model.last_messages[0].content
    assert "language is unknown or" in chat_model.last_messages[0].content
    assert "write the explanation in English" in chat_model.last_messages[0].content
    assert "sender_name: Ada Lovelace" in chat_model.last_messages[1].content
    assert "should_greet_customer: false" in chat_model.last_messages[1].content
    assert "greeting_reason: active conversation; avoid repeated greeting" in (
        chat_model.last_messages[1].content
    )


async def test_llm_question_planner_includes_tenant_planner_instructions() -> None:
    chat_model = FakeChatModel(
        '{"in_scope": true, "needs_conversation_history": false, '
        '"explanation": "tenant planner config"}'
    )
    planner = LlmQuestionPlanner(chat_model)

    await planner.plan(
        IncomingMessage(
            tenant_id="tenant-a",
            event_id="plan-tenant",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Do you serve tea?",
        ),
        tenant_config=TenantConfig(
            tenant_id="tenant-a",
            planner_prompt_instructions="Tenant A menu questions are in scope.",
        ),
    )

    system_prompt = chat_model.last_messages[0].content
    prompt = chat_model.last_messages[1].content
    assert "Tenant-specific planner instructions may customize" in system_prompt
    assert "Tenant-specific planner instructions" in prompt
    assert "Tenant A menu questions are in scope." in prompt


async def test_llm_question_planner_prompt_keeps_contextual_followups_in_scope() -> None:
    chat_model = FakeChatModel(
        '{"in_scope": true, "needs_conversation_history": true, '
        '"explanation": "current conversation follow-up"}'
    )
    planner = LlmQuestionPlanner(chat_model)

    plan = await planner.plan(
        IncomingMessage(
            event_id="plan-contextual",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Why did you speak Spanish?",
        ),
        ConversationPromptMetadata(
            is_first_customer_message=False,
            customer_name=None,
            minutes_since_last_customer_message=1,
            should_greet_customer=False,
            greeting_reason="active conversation; avoid repeated greeting",
        ),
    )

    system_prompt = chat_model.last_messages[0].content
    assert plan.in_scope is True
    assert plan.needs_conversation_history is True
    assert '"Why did you speak that language?"' in system_prompt
    assert "Do not tell the customer that their message" in system_prompt
    assert "depends on previous messages" in system_prompt
    assert "when did I first send you a message?" in system_prompt
    assert "what time" in system_prompt


async def test_llm_question_planner_uses_safe_defaults_for_invalid_json() -> None:
    chat_model = FakeChatModel("not-json")
    planner = LlmQuestionPlanner(chat_model)

    plan = await planner.plan(
        IncomingMessage(
            event_id="plan-2",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Can I still get that offer?",
        )
    )

    assert plan.in_scope is True
    assert plan.needs_conversation_history is True
    assert plan.explanation == "planner returned invalid JSON"


async def test_openai_answer_provider_requires_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await create_container(Settings(answer_provider="openai"))


async def test_openai_question_planner_provider_requires_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await create_container(Settings(question_planner_provider="llm"))


async def test_openai_embedding_provider_requires_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await create_container(Settings(embedding_provider="openai"))


def test_settings_read_openai_api_key_without_agent_prefix(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_OPENAI_API_KEY", "ignored-key")

    settings = Settings()

    assert settings.openai_api_key == "test-key"
