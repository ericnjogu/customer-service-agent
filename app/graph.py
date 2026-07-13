from typing import TypedDict

from langchain_core.documents import Document
from langgraph.graph import END, START, StateGraph

from app.knowledge import SEED_KNOWLEDGE_NAMESPACE
from app.models import ConversationRecord, IncomingMessage, StoredMessage, SupportReply
from app.ports import AnswerGenerator, ConversationRepository, RetrievalStore


class SupportState(TypedDict, total=False):
    message: IncomingMessage
    conversation: ConversationRecord
    conversation_history: list[StoredMessage]
    documents: list[Document]
    answer: str
    confidence: float
    citations: list[str]
    escalated: bool


def build_support_graph(
    conversations: ConversationRepository,
    retrieval: RetrievalStore,
    generator: AnswerGenerator,
    confidence_threshold: float,
    conversation_history_max_messages: int,
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

    async def load_conversation_history(state: SupportState) -> dict:
        conversation = state["conversation"]
        history = await conversations.list_messages_since(
            conversation.id,
            conversation.created_at,
            conversation_history_max_messages,
        )
        return {"conversation_history": history}

    async def retrieve(state: SupportState) -> dict:
        documents = await retrieval.search(state["message"].text, SEED_KNOWLEDGE_NAMESPACE)
        return {"documents": documents}

    async def answer(state: SupportState) -> dict:
        if state["conversation"].status == "HUMAN_ACTIVE":
            return {
                "answer": "This conversation is currently being handled by human support.",
                "confidence": 0.0,
                "citations": [],
                "escalated": True,
            }

        text, confidence = await generator.generate(
            state["message"].text,
            state["documents"],
            state.get("conversation_history", []),
        )
        citations = [str(item.metadata.get("source", "unknown")) for item in state["documents"]]
        return {
            "answer": text,
            "confidence": confidence,
            "citations": citations,
            "escalated": confidence < confidence_threshold,
        }

    async def persist_reply(state: SupportState) -> dict:
        conversation = state["conversation"]
        if state["escalated"] and conversation.status != "HUMAN_ACTIVE":
            conversation = await conversations.update_status(
                conversation.id,
                status="HANDOFF_PENDING",
                issue_status="ESCALATED",
                reason="Low confidence support graph answer",
            )
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
    workflow.add_node("load_conversation_history", load_conversation_history)
    workflow.add_node("retrieve", retrieve)
    workflow.add_node("answer", answer)
    workflow.add_node("persist_reply", persist_reply)
    workflow.add_edge(START, "persist_message")
    workflow.add_edge("persist_message", "load_conversation_history")
    workflow.add_edge("load_conversation_history", "retrieve")
    workflow.add_edge("retrieve", "answer")
    workflow.add_edge("answer", "persist_reply")
    workflow.add_edge("persist_reply", END)
    return workflow.compile()


async def invoke_support_graph(graph, message: IncomingMessage) -> SupportReply:
    state = await graph.ainvoke({"message": message})
    return SupportReply(
        conversation_id=state["conversation"].id,
        answer=state["answer"],
        confidence=state["confidence"],
        citations=state["citations"],
        escalated=state["escalated"],
        handling_status=state["conversation"].status,
        issue_status=state["conversation"].issue_status,
    )
