import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.adapters.llm import LlmAnswerGenerator, LlmHumanRequestDetector, LlmQuestionPlanner
from app.config import Settings
from app.container import create_container
from app.models import ConversationPromptMetadata, IncomingMessage, StoredMessage


class FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0
        self.last_messages = None

    async def ainvoke(self, messages):
        self.calls += 1
        self.last_messages = messages
        return AIMessage(content=self.content)


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
        [Document(page_content="Maxys Lounge serves food and drinks.")],
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
        [Document(page_content="Maxys Lounge closes at 8 PM.")],
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
            )
        ],
    )

    prompt = chat_model.last_messages[1].content
    assert "Conversation history for the current issue" in prompt
    assert "CUSTOMER: I need a refund." in prompt


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


def test_settings_read_openai_api_key_without_support_prefix(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SUPPORT_OPENAI_API_KEY", "ignored-key")

    settings = Settings()

    assert settings.openai_api_key == "test-key"
