import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.adapters.llm import (
    LlmAnswerGenerator,
    LlmQuestionPlanner,
    OpenAIWebsiteAnalyzer,
    TavilyRuntimeWebSearch,
    TavilyWebsiteResearcher,
    create_openai_answer_generator,
    create_openai_question_planner,
    format_age,
    format_extracted_links,
    langsmith_client,
    langsmith_runnable_config,
    langsmith_tracing_enabled,
    normalize_website_analysis_payload,
    tavily_project_id,
    tenant_trace_metadata,
    tenant_trace_tags,
    traced_responses_outputs,
)
from app.config import Settings
from app.container import create_container, create_runtime_web_search
from app.models import (
    ConversationPromptMetadata,
    IncomingMessage,
    OnboardingAdmin,
    OnboardingSessionRecord,
    StoredMessage,
    TenantConfig,
    WebsiteAnalysisResult,
)


async def test_runtime_web_search_defaults_to_noop_without_platform_api_key(
    monkeypatch,
) -> None:
    monkeypatch.delenv("AGENT_PLATFORM_WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    runtime_search = create_runtime_web_search(
        Settings(runtime_web_search_provider="tavily")
    )

    result = await runtime_search.search_answer(
        "Who works there?",
        TenantConfig.with_defaults("tenant-a"),
    )

    assert result.answer == ""
    assert result.sources == []


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


class FakeResponsesClient:
    def __init__(self, output_text: str | list[str]) -> None:
        self.output_texts = (
            list(output_text) if isinstance(output_text, list) else [output_text]
        )
        self.calls = 0
        self.last_kwargs = None
        self.call_kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        self.call_kwargs.append(kwargs)
        output_text = self.output_texts[min(self.calls - 1, len(self.output_texts) - 1)]
        return type("FakeResponse", (), {"output_text": output_text})()


class FakeWebsiteResearcher:
    def __init__(self, research_text: str) -> None:
        self.research_text = research_text
        self.calls: list[str] = []

    async def research(self, website_url: str) -> str:
        self.calls.append(website_url)
        return self.research_text


async def fake_link_extractor(website_url: str, *, timeout_seconds: float) -> str:
    return (
        f"- kind=facebook label=Facebook url={website_url}/facebook\n"
        "- kind=whatsapp label=WhatsApp url=https://wa.me/254700000000"
    )


def test_traced_responses_outputs_include_output_text() -> None:
    output = type(
        "FakeResponse",
        (),
        {
            "id": "resp_123",
            "output_text": "Website research notes",
        },
    )()

    assert traced_responses_outputs(output) == {
        "response_id": "resp_123",
        "output_text": "Website research notes",
        "output_text_chars": 22,
    }


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
        "vector_namespace": "tenant-a",
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


def test_langsmith_runnable_config_uses_tenant_project_destination(monkeypatch) -> None:
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
    assert captured_tracer["project_name"] == "customer-service-tenant-a"
    assert "tenant:tenant-a" in config["tags"]
    assert config["metadata"]["langsmith_project"] == "customer-service-tenant-a"

    langsmith_client.cache_clear()


async def test_openai_answer_generator_uses_tenant_project_header(monkeypatch) -> None:
    captured_requests = []

    async def fake_acompletion(**kwargs):
        captured_requests.append(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"answer": "Refunds are available within 30 days.", '
                            '"confidence": 0.82, "grounded": true}'
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    generator = create_openai_answer_generator(
        api_key="test-key",
        model="gpt-4.1-mini",
        temperature=0,
    )

    await generator.generate(
        "What is the refund policy?",
        [Document(page_content="Refunds are available within 30 days.")],
        tenant_config=TenantConfig.with_defaults(
            "tenant-a",
            llm_project_id="proj_tenant_a",
        ),
    )

    assert captured_requests[-1]["api_key"] == "test-key"
    assert captured_requests[-1]["model"] == "gpt-4.1-mini"
    assert captured_requests[-1]["response_format"] == {"type": "json_object"}
    assert captured_requests[-1]["extra_headers"] == {"OpenAI-Project": "proj_tenant_a"}


async def test_openai_question_planner_uses_tenant_project_header(monkeypatch) -> None:
    captured_requests = []

    async def fake_acompletion(**kwargs):
        captured_requests.append(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"in_scope": true, "needs_conversation_history": false, '
                            '"explicit_human_request": false, "explanation": "standalone"}'
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    planner = create_openai_question_planner(
        api_key="test-key",
        model="gpt-4.1-mini",
        temperature=0,
    )

    await planner.plan(
        IncomingMessage(
            event_id="plan-project",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Where are you located?",
        ),
        tenant_config=TenantConfig.with_defaults(
            "tenant-a",
            llm_project_id="proj_tenant_a",
        ),
    )

    assert captured_requests[-1]["api_key"] == "test-key"
    assert captured_requests[-1]["model"] == "gpt-4.1-mini"
    assert captured_requests[-1]["response_format"] == {"type": "json_object"}
    assert captured_requests[-1]["extra_headers"] == {"OpenAI-Project": "proj_tenant_a"}


async def test_openai_question_planner_omits_temperature_for_gpt_5(monkeypatch) -> None:
    captured_requests = []

    async def fake_acompletion(**kwargs):
        captured_requests.append(kwargs)
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"in_scope": true, "needs_conversation_history": false, '
                            '"explicit_human_request": false, "explanation": "standalone"}'
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    planner = create_openai_question_planner(
        api_key="test-key",
        model="gpt-5-mini",
        temperature=0,
    )

    await planner.plan(
        IncomingMessage(
            event_id="plan-gpt-5-temperature",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Where are you located?",
        ),
        tenant_config=TenantConfig.with_defaults("tenant-a"),
    )

    assert captured_requests[-1]["model"] == "gpt-5-mini"
    assert "temperature" not in captured_requests[-1]


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
    assert "answer_found: boolean" in chat_model.last_messages[0].content


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
            business_summary="Use Tenant A's warm brand voice.",
        ),
    )

    system_prompt = chat_model.last_messages[0].content
    prompt = chat_model.last_messages[1].content
    assert "Business summary and FAQ context may describe" in system_prompt
    assert "Business summary and FAQ" in prompt
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


async def test_llm_answer_generator_preserves_answer_found_signal() -> None:
    chat_model = FakeChatModel(
        '{"answer": "I do not have the team names in the available information.", '
        '"answer_found": false, "confidence": 0.86, "grounded": true}'
    )
    generator = LlmAnswerGenerator(chat_model)

    result = await generator.generate(
        "Who works there?",
        [Document(page_content="Hustle HQ has a WhatsApp contact link.")],
    )

    assert result.answer_found is False
    assert result.grounded is True
    assert result.confidence == 0.86


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


async def test_llm_question_planner_detects_explicit_human_request() -> None:
    chat_model = FakeChatModel(
        '{"in_scope": true, "needs_conversation_history": false, '
        '"explicit_human_request": true, "explanation": "customer asked for a human"}'
    )
    planner = LlmQuestionPlanner(chat_model)

    plan = await planner.plan(
        IncomingMessage(
            event_id="detect-1",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="Please connect me to a human agent.",
        )
    )

    assert plan.in_scope is True
    assert plan.explicit_human_request is True
    assert chat_model.calls == 1
    assert "Return explicit_human_request=true only when" in (
        chat_model.last_messages[0].content
    )


async def test_llm_question_planner_returns_structured_plan() -> None:
    chat_model = FakeChatModel(
        '{"in_scope": true, "needs_conversation_history": false, '
        '"explicit_human_request": false, "explanation": "standalone location question"}'
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
    assert plan.explicit_human_request is False
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
        '"explicit_human_request": false, "explanation": "tenant planner config"}'
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
            business_summary="## FAQ\n\nTenant A menu questions are in scope.",
        ),
    )

    system_prompt = chat_model.last_messages[0].content
    prompt = chat_model.last_messages[1].content
    assert "Business summary and FAQ context may describe" in system_prompt
    assert "Business summary and FAQ" in prompt
    assert "Tenant A menu questions are in scope." in prompt


async def test_llm_question_planner_prompt_keeps_contextual_followups_in_scope() -> None:
    chat_model = FakeChatModel(
        '{"in_scope": true, "needs_conversation_history": true, '
        '"explicit_human_request": false, "explanation": "current conversation follow-up"}'
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
    assert plan.explicit_human_request is False
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


def test_website_analysis_keeps_contact_uris() -> None:
    payload = normalize_website_analysis_payload(
        {
            "business_profile": {
                "business_name": "Hustle HQ",
                "website_url": "https://hustlehq.example",
                "location_name": "Hustle HQ",
                "physical_location": "Enterprise Road",
                "business_phone": "+254 700 000000",
                "business_email": "hello@hustlehq.example",
            },
            "business_summary": "Represent Hustle HQ.",
            "contact_info": [
                {
                    "kind": "email",
                    "label": "Email",
                    "value": "hello@hustlehq.example",
                    "url": "mailto:hello@hustlehq.example",
                    "is_primary": True,
                },
                {
                    "kind": "phone",
                    "label": "Phone",
                    "value": "+254 700 000000",
                    "url": "tel:+254700000000",
                    "is_primary": True,
                },
            ],
        },
        fallback_website_url="https://hustlehq.example",
    )

    assert [link["kind"] for link in payload["contact_info"]] == [
        "website",
        "email",
        "phone",
    ]
    assert payload["contact_info"][1]["url"] == "mailto:hello@hustlehq.example"
    assert payload["contact_info"][2]["url"] == "tel:+254700000000"

    analysis = WebsiteAnalysisResult.model_validate(payload)

    assert analysis.contact_info[1].url == "mailto:hello@hustlehq.example"
    assert analysis.contact_info[2].url == "tel:+254700000000"


def test_website_analysis_deduplicates_equivalent_contact_urls() -> None:
    payload = normalize_website_analysis_payload(
        {
            "business_profile": {
                "business_name": "Hustle HQ",
                "website_url": "https://hustlehq.example",
            },
            "contact_info": [
                {
                    "kind": "website",
                    "label": "Website",
                    "url": "[https://hustlehq.example/](https://hustlehq.example/)",
                },
                {
                    "kind": "website",
                    "label": "Hustle HQ website",
                    "url": "https://hustlehq.example",
                },
                {
                    "kind": "website",
                    "label": "Start onboarding",
                    "url": "https://hustlehq.example/onboarding",
                },
                {
                    "kind": "facebook",
                    "label": "Facebook",
                    "url": (
                        "[https://www.facebook.com/hustlehq]"
                        "(https://www.facebook.com/hustlehq)"
                    ),
                },
            ],
        },
        fallback_website_url="https://hustlehq.example",
    )

    assert [
        link["url"] for link in payload["contact_info"] if link["kind"] == "website"
    ] == ["https://hustlehq.example/"]
    assert payload["contact_info"][1]["url"] == "https://www.facebook.com/hustlehq"


def test_website_analysis_leaves_unknown_fields_blank() -> None:
    payload = normalize_website_analysis_payload(
        {
            "business_profile": {
                "website_url": "https://hustlehq.example",
            },
            "business_summary": "Represent Hustle HQ.",
            "contact_info": [],
        },
        fallback_website_url="https://hustlehq.example",
    )

    analysis = WebsiteAnalysisResult.model_validate(payload)

    assert analysis.business_profile.business_name == ""
    assert analysis.business_profile.location_name == ""
    assert analysis.business_profile.physical_location == ""
    assert analysis.business_profile.business_phone == ""
    assert analysis.business_profile.business_email == ""


async def test_website_analysis_prompt_only_includes_website_url() -> None:
    responses_client = FakeResponsesClient(
        json.dumps(
            {
                "business_profile": {
                    "website_url": "https://hustlehq.example",
                },
                "business_summary": "Represent Hustle HQ.",
                "contact_info": [],
            },
        )
    )
    researcher = FakeWebsiteResearcher("Hustle HQ is a business website.")
    analyzer = OpenAIWebsiteAnalyzer(
        responses_client,
        model="gpt-4.1-mini",
        website_researcher=researcher,
        link_extractor=fake_link_extractor,
    )

    await analyzer.analyze(
        OnboardingSessionRecord(
            website_url="https://hustlehq.example",
            admin=OnboardingAdmin(
                name="John Doe",
                email="admin@hustlehq.example",
                phone_number="+254110101010",
                role_title="Owner",
                authority_confirmed=True,
                terms_accepted=True,
            ),
        )
    )

    structure_call = responses_client.call_kwargs[0]
    assert responses_client.calls == 1
    assert researcher.calls == ["https://hustlehq.example"]
    assert structure_call["input"].startswith("Website URL: https://hustlehq.example")
    assert "Locally extracted contact links" in structure_call["input"]
    assert "https://wa.me/254700000000" in structure_call["input"]
    assert "Platform web search notes" in structure_call["input"]
    assert "Hustle HQ is a business website." in structure_call["input"]
    assert "Return JSON only" in structure_call["input"]
    assert "tools" not in structure_call
    assert structure_call["text"] == {
        "format": {
            "type": "json_object",
        }
    }
    assert structure_call["model"] == "gpt-4.1-mini"
    assert "temperature" not in structure_call
    assert "John Doe" not in structure_call["input"]
    assert "admin@hustlehq.example" not in structure_call["input"]


async def test_website_analysis_can_optionally_send_temperature() -> None:
    responses_client = FakeResponsesClient(
        json.dumps(
            {
                "business_profile": {
                    "website_url": "https://hustlehq.example",
                },
                "business_summary": "",
                "contact_info": [],
            },
        )
    )
    analyzer = OpenAIWebsiteAnalyzer(
        responses_client,
        model="gpt-4.1-mini",
        temperature=0.0,
        website_researcher=FakeWebsiteResearcher("Hustle HQ is a business website."),
        link_extractor=fake_link_extractor,
    )

    await analyzer.analyze(
        OnboardingSessionRecord(
            website_url="https://hustlehq.example",
            admin=OnboardingAdmin(
                name="John Doe",
                email="admin@hustlehq.example",
                phone_number="+254110101010",
                role_title="Owner",
                authority_confirmed=True,
                terms_accepted=True,
            ),
        )
    )

    assert responses_client.call_kwargs[0]["temperature"] == 0.0


def test_format_extracted_links_keeps_contact_links_only() -> None:
    output = format_extracted_links(
        [
            ("Home", "https://hustlehq.example/"),
            ("Facebook", "https://www.facebook.com/hustlehq"),
            ("Map", "https://maps.google.com/?q=Hustle+HQ"),
            ("Call", "tel:+254700000000"),
            ("Email", "mailto:hello@hustlehq.example"),
            ("Duplicate", "mailto:hello@hustlehq.example"),
        ]
    )

    assert "https://www.facebook.com/hustlehq" in output
    assert "https://maps.google.com/?q=Hustle+HQ" in output
    assert "tel:+254700000000" in output
    assert "mailto:hello@hustlehq.example" in output
    assert "https://hustlehq.example/" not in output
    assert output.count("mailto:hello@hustlehq.example") == 1


async def test_tavily_research_sends_project_id_and_returns_sources(
    monkeypatch,
) -> None:
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "results": [
                    {
                        "title": "Hustle HQ",
                        "url": "https://hustlehq.example/about",
                        "content": "Hustle HQ provides customer service.",
                        "raw_content": "Contact Hustle HQ on WhatsApp.",
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs) -> FakeResponse:
            calls.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setattr("app.adapters.llm.httpx.AsyncClient", FakeAsyncClient)

    researcher = TavilyWebsiteResearcher(
        api_key="test-tavily-key",
        project_id="ristoh-css",
        max_results=3,
        timeout_seconds=15,
    )

    output = await researcher.research("https://hustlehq.example")

    assert len(calls) == 1
    assert calls[0][0] == "https://api.tavily.com/search"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-tavily-key"
    assert calls[0][1]["headers"]["X-Project-ID"] == "ristoh-css"
    assert calls[0][1]["json"]["include_domains"] == ["hustlehq.example"]
    assert "Hustle HQ provides customer service." in output.notes
    assert len(output.sources) == 1
    assert output.sources[0].provider == "tavily"
    assert output.sources[0].url == "https://hustlehq.example/about"


async def test_tavily_runtime_web_search_sends_answer_and_project_headers(monkeypatch) -> None:
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "answer": "Hustle HQ has a public team page.",
                "results": [
                    {
                        "title": "Hustle HQ Team",
                        "url": "https://hustlehq.example/team",
                        "content": "Meet the Hustle HQ team.",
                        "raw_content": "# Team\nMeet the Hustle HQ team.",
                    }
                ],
            }

    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs) -> FakeResponse:
            calls.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setattr("app.adapters.llm.httpx.AsyncClient", FakeAsyncClient)

    search = TavilyRuntimeWebSearch(
        api_key="test-tavily-key",
        max_results=3,
        timeout_seconds=15,
    )

    result = await search.search_answer(
        "Who works at Hustle HQ?",
        TenantConfig.with_defaults(
            "tenant-a",
            web_search_project_name="tenant-a-project",
        ),
        "https://www.hustlehq.example",
    )

    assert len(calls) == 1
    assert calls[0][0] == "https://api.tavily.com/search"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-tavily-key"
    assert calls[0][1]["headers"]["X-Project-ID"] == "tenant-a-project"
    assert calls[0][1]["json"]["query"] == "Who works at Hustle HQ?"
    assert calls[0][1]["json"]["search_depth"] == "advanced"
    assert calls[0][1]["json"]["include_answer"] == "basic"
    assert calls[0][1]["json"]["include_raw_content"] == "markdown"
    assert calls[0][1]["json"]["include_domains"] == ["hustlehq.example"]
    assert calls[0][1]["json"]["max_results"] == 3
    assert result.answer == "Hustle HQ has a public team page."
    assert len(result.sources) == 1
    assert result.sources[0].url == "https://hustlehq.example/team"
    assert result.sources[0].provider == "tavily"


async def test_tavily_runtime_web_search_http_error_returns_empty_result(
    monkeypatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr("app.adapters.llm.httpx.AsyncClient", FakeAsyncClient)

    search = TavilyRuntimeWebSearch(
        api_key="test-tavily-key",
        max_results=3,
        timeout_seconds=15,
    )

    result = await search.search_answer("Who works there?", TenantConfig.with_defaults("t1"))

    assert result.answer == ""
    assert result.sources == []


def test_tavily_project_id_prefers_tenant_web_search_project_name() -> None:
    assert (
        tavily_project_id(
            TenantConfig.with_defaults(
                "tenant-a",
                web_search_project_name="tenant-a-project",
            )
        )
        == "tenant-a-project"
    )
    assert tavily_project_id(TenantConfig.with_defaults("tenant-a")) == "tenant-a"
    assert tavily_project_id(None) == "default"
