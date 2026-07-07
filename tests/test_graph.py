from app.config import Settings
from app.container import create_container
from app.graph import invoke_support_graph
from app.knowledge import SEED_KNOWLEDGE_NAMESPACE
from app.models import IncomingMessage


async def test_grounded_question_returns_citation() -> None:
    container = await create_container(Settings())
    reply = await invoke_support_graph(
        container.graph,
        IncomingMessage(
            event_id="event-1",
            external_chat_id="chat-1",
            external_user_id="user-1",
            text="How do I reset my password?",
        ),
    )

    assert reply.escalated is False
    assert reply.citations == ["kb/password-reset"]
    assert "Settings" in reply.answer


async def test_unknown_question_is_marked_for_escalation() -> None:
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

    assert reply.escalated is True
    assert reply.confidence == 0.0
    assert reply.citations == []


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

    assert reply.escalated is False
    assert reply.citations == ["kb/shipping.md"]
    assert "Shipping address changes" in reply.answer


async def test_seed_knowledge_namespace_constant_is_used() -> None:
    container = await create_container(Settings())

    assert SEED_KNOWLEDGE_NAMESPACE == "seed-knowledge"
    assert SEED_KNOWLEDGE_NAMESPACE in container.retrieval.documents
    assert "knowledge" not in container.retrieval.documents
