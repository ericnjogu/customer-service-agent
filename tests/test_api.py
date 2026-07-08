from fastapi.testclient import TestClient

from app.main import app


def test_synthetic_webhook_vertical_slice() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/synthetic",
            json={
                "event_id": "api-event-1",
                "external_chat_id": "chat-1",
                "external_user_id": "user-1",
                "text": "What is the refund policy?",
            },
        )

    assert response.status_code == 200
    assert response.json()["citations"] == ["kb/refunds"]


def test_conversation_status_can_be_read_and_updated() -> None:
    with TestClient(app) as client:
        webhook_response = client.post(
            "/webhooks/synthetic",
            json={
                "event_id": "api-event-2",
                "external_chat_id": "chat-2",
                "external_user_id": "user-2",
                "text": "Please explain a topic that is not in the knowledge base.",
            },
        )
        conversation_id = webhook_response.json()["conversation_id"]

        get_response = client.get(f"/conversations/{conversation_id}")
        update_response = client.patch(
            f"/conversations/{conversation_id}/status",
            json={
                "status": "HUMAN_ACTIVE",
                "issue_status": "IN_PROGRESS",
                "reason": "Human support accepted the handoff",
            },
        )

    assert webhook_response.status_code == 200
    assert webhook_response.json()["handling_status"] == "HANDOFF_PENDING"
    assert webhook_response.json()["issue_status"] == "ESCALATED"
    assert get_response.status_code == 200
    assert get_response.json()["status"] == "HANDOFF_PENDING"
    assert get_response.json()["issue_status"] == "ESCALATED"
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "HUMAN_ACTIVE"
    assert update_response.json()["issue_status"] == "IN_PROGRESS"
