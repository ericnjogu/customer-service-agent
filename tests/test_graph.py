from app.config import Settings
from app.container import create_container
from app.graph import invoke_support_graph
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
