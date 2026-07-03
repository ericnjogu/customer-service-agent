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
