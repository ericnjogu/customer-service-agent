import json
import logging
from typing import Any

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage, SystemMessage

from app.models import ConversationPromptMetadata, IncomingMessage, StoredMessage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a customer support assistant.
Answer only from the provided information.
Only answer questions about the business, its services, policies, products, orders,
bookings, support process, or the customer's current support conversation.
Do not answer general-purpose questions, trivia, math problems, riddles, coding questions,
or unrelated requests, even if you know the answer.
Use only the latest customer question to choose the response language. Do not infer the
response language from conversation history, previous assistant replies, or knowledge-base
context. If the latest customer question's language is unclear, reply in English. The
knowledge base may be in a different language; translate or summarize the grounded answer
into the latest customer question's language while keeping names, product names, place
names, phone numbers, URLs, and quoted text unchanged unless translation is necessary for
clarity.
Each retrieved knowledge chunk includes a created_at timestamp. If multiple relevant
chunks overlap or conflict, prefer the chunk with the newer created_at timestamp. Do not
use a newer chunk merely because it is newer; it must still be relevant to the customer's
question.
Use conversation metadata to decide whether to greet the customer. Do not repeatedly greet
the customer during an active back-and-forth. 
If customer_name is provided, use that name naturally when greeting or addressing the
customer. Do not invent a customer name when customer_name is none.
Welcome them back if it has been a significant time since their most recent post.
If the context is insufficient, say that you do not have enough information 
and ask if they would like to contact a support team member.
If they ask for a human agent or support team member or management, send them the
contact information.
For unrelated or out-of-scope questions, return a short answer explaining that you are
here to help with questions about this business, set confidence to 0, and set grounded
to false.
Return JSON with:
- answer: string
- confidence: number from 0 to 1
- grounded: boolean
"""

HUMAN_REQUEST_DETECTION_PROMPT = """Determine whether the customer's latest message
explicitly asks to speak with a human support person.
Return true only when the customer clearly asks for a human agent, real person, support
team member, manager, or escalation to a person.
Return false for low-confidence situations, unanswered questions, complaints,
frustration, or negative sentiment that do not ask for a person.
Return JSON only with:
- explicit_human_request: boolean
"""


def format_context(documents: list[Document]) -> str:
    if not documents:
        return "No knowledge base context was retrieved."

    chunks = []
    for index, document in enumerate(documents, start=1):
        source = str(document.metadata.get("source", "unknown"))
        chunk_id = str(document.metadata.get("chunk_id") or source)
        created_at = str(document.metadata.get("created_at", "unknown"))
        chunks.append(
            f"[{index}] chunk_id={chunk_id} source={source} created_at={created_at}\n"
            f"{document.page_content}"
        )
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
            f"customer_name: {metadata.customer_name or 'none'}",
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
                    "Response language instruction:\n"
                    "Use the language of the latest customer question below. "
                    "Ignore the language of conversation history, previous assistant "
                    "replies, and knowledge-base context when choosing the response "
                    "language. If the knowledge base uses a different language, "
                    "translate or summarize the answer into the latest customer "
                    "question's language.\n\n"
                    f"Latest customer question:\n{query}"
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


class LlmHumanRequestDetector:
    def __init__(self, chat_model: Any) -> None:
        self.chat_model = chat_model

    async def detect(
        self,
        message: IncomingMessage,
        conversation_history: list[StoredMessage] | None = None,
    ) -> bool:
        history = conversation_history or []
        messages = [
            SystemMessage(content=HUMAN_REQUEST_DETECTION_PROMPT),
            HumanMessage(
                content=(
                    "Conversation history for the current issue:\n"
                    f"{format_conversation_history(history)}\n\n"
                    "Latest customer message:\n"
                    f"{message.text}"
                )
            ),
        ]
        response = await self.chat_model.ainvoke(messages)
        content = str(response.content)

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("LLM human-request detection was not valid JSON; defaulting false")
            return False

        return bool(payload.get("explicit_human_request", False))


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


def create_openai_human_request_detector(
    *,
    api_key: str,
    model: str,
    temperature: float,
) -> LlmHumanRequestDetector:
    from langchain_openai import ChatOpenAI

    chat_model = ChatOpenAI(
        api_key=api_key,
        model=model,
        temperature=temperature,
        model_kwargs={"response_format": {"type": "json_object"}},
    )
    return LlmHumanRequestDetector(chat_model)
