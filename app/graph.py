from typing import TypedDict

from langchain_core.documents import Document
from langgraph.graph import END, START, StateGraph

from app.knowledge import SEED_KNOWLEDGE_NAMESPACE
from app.models import (
    ConversationPromptMetadata,
    ConversationRecord,
    IncomingMessage,
    StoredMessage,
    SupportReply,
)
from app.ports import (
    AnswerGenerator,
    ConversationRepository,
    HumanRequestDetector,
    RetrievalStore,
)


class SupportState(TypedDict, total=False):
    message: IncomingMessage
    conversation: ConversationRecord
    conversation_history: list[StoredMessage]
    conversation_metadata: ConversationPromptMetadata
    documents: list[Document]
    answer: str
    confidence: float
    citations: list[str]
    low_confidence: bool
    human_requested: bool


def build_support_graph(
    conversations: ConversationRepository,
    retrieval: RetrievalStore,
    generator: AnswerGenerator,
    human_request_detector: HumanRequestDetector,
    confidence_threshold: float,
    conversation_history_max_messages: int,
    greeting_lapse_minutes: int,
):
    async def persist_message(state: SupportState) -> dict:
        conversation = await conversations.get_or_create(state["message"])
        await conversations.save_message(
            StoredMessage(
                conversation_id=conversation.id,
                event_id=state["message"].event_id,
                sender_type="CUSTOMER",
                body=state["message"].text,
            )
        )
        return {"conversation": conversation}

    async def detect_human_request(state: SupportState) -> dict:
        human_requested = await human_request_detector.detect(state["message"])
        return {"human_requested": human_requested}

    async def apply_human_request_state(state: SupportState) -> dict:
        conversation = state["conversation"]
        if state.get("human_requested") and conversation.state == "BOT_ACTIVE":
            conversation = await conversations.update_state(
                conversation.id,
                state="HUMAN_REQUESTED",
                reason="Customer explicitly requested human support",
            )
        return {"conversation": conversation}

    async def load_conversation_history(state: SupportState) -> dict:
        conversation = state["conversation"]
        history = await conversations.list_messages_since(
            conversation.id,
            conversation.created_at,
            conversation_history_max_messages,
        )
        return {"conversation_history": history}

    async def build_conversation_metadata(state: SupportState) -> dict:
        metadata = build_prompt_metadata(
            state["message"],
            state.get("conversation_history", []),
            greeting_lapse_minutes,
        )
        return {"conversation_metadata": metadata}

    async def retrieve(state: SupportState) -> dict:
        documents = await retrieval.search(state["message"].text, SEED_KNOWLEDGE_NAMESPACE)
        return {"documents": documents}

    async def answer(state: SupportState) -> dict:
        if state["conversation"].state == "HUMAN_ACTIVE":
            return {
                "answer": "This conversation is currently being handled by human support.",
                "confidence": 0.0,
                "citations": [],
                "low_confidence": True,
            }
        text, confidence = await generator.generate(
            state["message"].text,
            state["documents"],
            state.get("conversation_history", []),
            state.get("conversation_metadata"),
        )
        citations = [
            str(item.metadata.get("chunk_id") or item.metadata.get("source", "unknown"))
            for item in state["documents"]
        ]
        return {
            "answer": text,
            "confidence": confidence,
            "citations": citations,
            "low_confidence": confidence < confidence_threshold,
        }

    async def persist_reply(state: SupportState) -> dict:
        conversation = state["conversation"]
        await conversations.save_message(
            StoredMessage(
                conversation_id=conversation.id,
                event_id=f"reply:{state['message'].event_id}",
                sender_type="BOT",
                body=state["answer"],
            )
        )
        return {"conversation": conversation}

    workflow = StateGraph(SupportState)
    workflow.add_node("persist_message", persist_message)
    workflow.add_node("detect_human_request", detect_human_request)
    workflow.add_node("apply_human_request_state", apply_human_request_state)
    workflow.add_node("load_conversation_history", load_conversation_history)
    workflow.add_node("build_conversation_metadata", build_conversation_metadata)
    workflow.add_node("retrieve", retrieve)
    workflow.add_node("answer", answer)
    workflow.add_node("persist_reply", persist_reply)
    workflow.add_edge(START, "persist_message")
    workflow.add_edge("persist_message", "detect_human_request")
    workflow.add_edge("detect_human_request", "apply_human_request_state")
    workflow.add_edge("apply_human_request_state", "load_conversation_history")
    workflow.add_edge("load_conversation_history", "build_conversation_metadata")
    workflow.add_edge("build_conversation_metadata", "retrieve")
    workflow.add_edge("retrieve", "answer")
    workflow.add_edge("answer", "persist_reply")
    workflow.add_edge("persist_reply", END)
    return workflow.compile()


def build_prompt_metadata(
    current_message: IncomingMessage,
    conversation_history: list[StoredMessage],
    greeting_lapse_minutes: int,
) -> ConversationPromptMetadata:
    previous_customer_messages = [
        message
        for message in conversation_history
        if message.sender_type == "CUSTOMER" and message.event_id != current_message.event_id
    ]
    previous_customer_message = (
        previous_customer_messages[-1] if previous_customer_messages else None
    )

    if previous_customer_message is None:
        return ConversationPromptMetadata(
            is_first_customer_message=True,
            should_greet_customer=True,
            greeting_reason="first customer message in this conversation",
        )

    minutes_since_last_customer_message = max(
        0,
        int(
            (
                current_message.received_at - previous_customer_message.created_at
            ).total_seconds()
            // 60
        ),
    )
    should_greet_customer = minutes_since_last_customer_message >= greeting_lapse_minutes
    greeting_reason = (
        f"last customer message was {minutes_since_last_customer_message} minutes ago"
        if should_greet_customer
        else "active conversation; avoid repeated greeting"
    )
    return ConversationPromptMetadata(
        is_first_customer_message=False,
        minutes_since_last_customer_message=minutes_since_last_customer_message,
        should_greet_customer=should_greet_customer,
        greeting_reason=greeting_reason,
    )


async def invoke_support_graph(graph, message: IncomingMessage) -> SupportReply:
    state = await graph.ainvoke({"message": message})
    return SupportReply(
        conversation_id=state["conversation"].id,
        answer=state["answer"],
        confidence=state["confidence"],
        citations=state["citations"],
        low_confidence=state["low_confidence"],
        state=state["conversation"].state,
    )
