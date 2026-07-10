import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from app.adapters.llm import LlmAnswerGenerator
from app.config import Settings
from app.container import create_container


class FakeChatModel:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
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


async def test_llm_answer_generator_does_not_call_model_without_documents() -> None:
    chat_model = FakeChatModel('{"answer": "unused", "confidence": 1, "grounded": true}')
    generator = LlmAnswerGenerator(chat_model)

    answer, confidence = await generator.generate("Unknown?", [])

    assert answer == "I could not find enough information to answer that safely."
    assert confidence == 0.0
    assert chat_model.calls == 0


async def test_openai_answer_provider_requires_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await create_container(Settings(answer_provider="openai"))


def test_settings_read_openai_api_key_without_support_prefix(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SUPPORT_OPENAI_API_KEY", "ignored-key")

    settings = Settings()

    assert settings.openai_api_key == "test-key"
