import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


class FakeTelegramSender:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> None:
        self.sent_messages.append((chat_id, text))


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_synthetic_webhook_vertical_slice(tmp_path, monkeypatch) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "refunds.txt").write_text(
        "Refund requests can be submitted within 30 days of purchase.",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPPORT_KNOWLEDGE_PATH", str(knowledge_dir))

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
    assert response.json()["citations"] == ["kb/refunds.txt"]


def test_customer_message_endpoint_receives_user_question(tmp_path, monkeypatch) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "password-reset.txt").write_text(
        "To reset your password, open Settings, select Security, then choose Reset password.",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPPORT_KNOWLEDGE_PATH", str(knowledge_dir))

    with TestClient(app) as client:
        response = client.post(
            "/messages/customer",
            json={
                "event_id": "api-event-customer-message-1",
                "external_chat_id": "chat-customer-message-1",
                "external_user_id": "user-customer-message-1",
                "text": "How do I reset my password?",
            },
        )

    assert response.status_code == 200
    assert response.json()["citations"] == ["kb/password-reset.txt"]
    assert response.json()["handling_status"] == "BOT_ACTIVE"


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


def test_telegram_webhook_receives_customer_message_and_sends_reply(
    tmp_path,
    monkeypatch,
) -> None:
    knowledge_dir = tmp_path / "knowledge"
    knowledge_dir.mkdir()
    (knowledge_dir / "refunds.txt").write_text(
        "Refund requests can be submitted within 30 days of purchase.",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUPPORT_KNOWLEDGE_PATH", str(knowledge_dir))

    sender = FakeTelegramSender()
    with TestClient(app) as client:
        client.app.state.container.telegram_sender = sender
        response = client.post(
            "/webhooks/telegram",
            json={
                "update_id": 1001,
                "message": {
                    "message_id": 2002,
                    "chat": {"id": 3003},
                    "from": {"id": 4004, "first_name": "Ada"},
                    "text": "What is the refund policy?",
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["reply"]["citations"] == ["kb/refunds.txt"]
    assert sender.sent_messages == [
        ("3003", "Refund requests can be submitted within 30 days of purchase.")
    ]


def test_telegram_webhook_rejects_invalid_secret_token(monkeypatch) -> None:
    monkeypatch.setenv("SUPPORT_TELEGRAM_WEBHOOK_SECRET_TOKEN", "expected-secret")

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
            json={"update_id": 1001},
        )

    assert response.status_code == 403


def test_telegram_webhook_ignores_non_text_updates() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/telegram",
            json={
                "update_id": 1001,
                "message": {
                    "message_id": 2002,
                    "chat": {"id": 3003},
                    "from": {"id": 4004},
                    "photo": [{"file_id": "photo-id"}],
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}
