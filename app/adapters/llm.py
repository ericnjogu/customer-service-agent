import json
import logging
from typing import Any

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from app.models import ConversationPromptMetadata, StoredMessage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a customer support assistant.
Answer only from the provided knowledge base context.
Use conversation metadata to decide whether to greet the customer. Do not repeatedly greet
the customer during an active back-and-forth. 
Welcome them back if it has been a significant time since their most recent post.
If the context is insufficient, say that you do not have enough information 
and ask if they would like to contact a support team member.
If they ask for a human agent or support team member or management, send them the
contact information in the knowledge base.
Return JSON with:
- answer: string
- confidence: number from 0 to 1
- grounded: boolean
"""


def format_context(documents: list[Document]) -> str:
    if not documents:
        return "No knowledge base context was retrieved."

    chunks = []
    for index, document in enumerate(documents, start=1):
        source = str(document.metadata.get("source", "unknown"))
        chunks.append(f"[{index}] source={source}\n{document.page_content}")
    return "\n\n".join(chunks)


def format_conversation_history(messages: list[StoredMessage]) -> str:
    if not messages:
        return "No prior conversation history is available."

    return "\n".join(
        f"{message.sender_type}: {message.body}"
        for message in messages
    )


def format_conversation_metadata(metadata: ConversationPromptMetadata | None) -> str:
    if metadata is None:
        return "No conversation metadata is available."

    minutes = (
        str(metadata.minutes_since_last_customer_message)
        if metadata.minutes_since_last_customer_message is not None
        else "none"
    )
    return "\n".join(
        [
            f"is_first_customer_message: {str(metadata.is_first_customer_message).lower()}",
            f"minutes_since_last_customer_message: {minutes}",
            f"should_greet_customer: {str(metadata.should_greet_customer).lower()}",
            f"greeting_reason: {metadata.greeting_reason}",
        ]
    )


class LlmAnswerGenerator:
    def __init__(self, chat_model: Any) -> None:
        self.chat_model = chat_model

    async def generate(
        self,
        query: str,
        documents: list[Document],
        conversation_history: list[StoredMessage] | None = None,
        conversation_metadata: ConversationPromptMetadata | None = None,
    ) -> tuple[str, float]:
        if not documents:
            return "I could not find enough information to answer that question.", 0.0

        history = conversation_history or []
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    "Conversation metadata:\n"
                    f"{format_conversation_metadata(conversation_metadata)}\n\n"
                    "Conversation history for the current issue:\n"
                    f"{format_conversation_history(history)}\n\n"
                    "Knowledge base context:\n"
                    f"{format_context(documents)}\n\n"
                    f"Customer question:\n{query}"
                )
            ),
        ]
        response = await self.chat_model.ainvoke(messages)
        content = str(response.content)

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("LLM answer was not valid JSON; escalating response")
            return "I could not verify the answer well enough to respond safely.", 0.0

        answer = str(payload.get("answer", "")).strip()
        grounded = bool(payload.get("grounded", False))
        confidence = float(payload.get("confidence", 0.0))

        if not answer:
            return "I could not find enough information to answer that safely.", 0.0
        if not grounded:
            return answer, min(confidence, 0.3)
        return answer, max(0.0, min(confidence, 0.95))


def create_openai_answer_generator(
    *,
    api_key: str,
    model: str,
    temperature: float,
) -> LlmAnswerGenerator:
    from langchain_openai import ChatOpenAI

    chat_model = ChatOpenAI(
        api_key=api_key,
        model=model,
        temperature=temperature,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    return LlmAnswerGenerator(chat_model)
