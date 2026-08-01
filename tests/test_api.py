import asyncio

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document

from app.config import get_settings
from app.knowledge import SEED_KNOWLEDGE_NAMESPACE
from app.main import app


def validation_messages(response) -> list[str]:
    return [str(error.get("msg", "")) for error in response.json()["detail"]]


def seed_default_knowledge(client: TestClient, filename: str, content: str) -> None:
    source = f"kb/{filename}"
    asyncio.run(
        client.app.state.container.retrieval.upsert(
            [
                Document(
                    page_content=content,
                    metadata={
                        "source": source,
                        "chunk_id": f"{source}#0000",
                    },
                )
            ],
            SEED_KNOWLEDGE_NAMESPACE,
        )
    )


class FakeTelegramSender:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str, str]] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        tenant_id: str = "default",
    ) -> None:
        self.sent_messages.append((chat_id, text, tenant_id))


class FakeWhatsAppSender:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[str, str, str]] = []

    async def send_message(
        self,
        to: str,
        text: str,
        tenant_id: str = "default",
    ) -> None:
        self.sent_messages.append((to, text, tenant_id))


class FakeTelegramCredentials:
    def __init__(self, webhook_secret_token: str | None = None) -> None:
        self.bot_token = "fake-token"
        self.webhook_secret_token = webhook_secret_token


class FakeTelegramCredentialResolver:
    def __init__(self, webhook_secret_token: str | None = None) -> None:
        self.webhook_secret_token = webhook_secret_token
        self.tenant_ids: list[str] = []

    async def resolve(self, tenant_id: str) -> FakeTelegramCredentials:
        self.tenant_ids.append(tenant_id)
        return FakeTelegramCredentials(self.webhook_secret_token)


class FakeWhatsAppCredentials:
    def __init__(self, verify_token: str | None = None) -> None:
        self.access_token = "fake-token"
        self.phone_number_id = "fake-phone-number-id"
        self.verify_token = verify_token
        self.graph_api_version = "v20.0"


class FakeWhatsAppCredentialResolver:
    def __init__(self, verify_token: str | None = None) -> None:
        self.verify_token = verify_token
        self.tenant_ids: list[str] = []

    async def resolve(self, tenant_id: str) -> FakeWhatsAppCredentials:
        self.tenant_ids.append(tenant_id)
        return FakeWhatsAppCredentials(self.verify_token)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_synthetic_webhook_vertical_slice() -> None:
    with TestClient(app) as client:
        seed_default_knowledge(
            client,
            "refunds.txt",
            "Refund requests can be submitted within 30 days of purchase.",
        )
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
    assert response.json()["citations"] == ["kb/refunds.txt#0000"]


def test_customer_message_endpoint_receives_user_question() -> None:
    with TestClient(app) as client:
        seed_default_knowledge(
            client,
            "password-reset.txt",
            "To reset your password, open Settings, select Security, then choose Reset password.",
        )
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
    assert response.json()["citations"] == ["kb/password-reset.txt#0000"]
    assert response.json()["state"] == "BOT_ACTIVE"


def test_customer_message_endpoint_accepts_tenant_header() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/messages/customer",
            headers={"X-Agent-Tenant-Id": "tenant-a"},
            json={
                "event_id": "api-tenant-event-1",
                "external_chat_id": "api-tenant-chat-1",
                "external_user_id": "api-tenant-user-1",
                "text": "Unknown question?",
            },
        )

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"


def test_tenant_can_be_created_with_generated_id_without_config() -> None:
    with TestClient(app) as client:
        create_response = client.post(
            "/tenants",
            json={
                "display_name": "Hustle HQ",
                "selected_plan": "enterprise",
            },
        )
        tenant_id = create_response.json()["tenant_id"]
        get_response = client.get(f"/tenants/{tenant_id}")
        config_response = client.get(f"/tenants/{tenant_id}/config")

    assert create_response.status_code == 201
    assert tenant_id.startswith("tnt_")
    assert "config" not in create_response.json()
    assert create_response.json()["slug"] == "hustle-hq"
    assert create_response.json()["display_name"] == "Hustle HQ"
    assert create_response.json()["selected_plan"] == "enterprise"
    assert get_response.status_code == 200
    assert get_response.json()["tenant_id"] == tenant_id
    assert config_response.status_code == 404
    assert config_response.json()["detail"] == "Tenant config not found"


def test_tenant_creation_returns_conflict_when_slug_already_exists() -> None:
    with TestClient(app) as client:
        first_response = client.post("/tenants", json={"display_name": "Hustle HQ"})
        second_response = client.post("/tenants", json={"display_name": "Hustle HQ"})

    assert first_response.status_code == 201
    assert second_response.status_code == 409
    assert first_response.json()["slug"] == "hustle-hq"
    assert (
        second_response.json()["detail"]
        == "Tenant with matching name or slug already exists"
    )


def test_tenant_can_be_read_by_slug() -> None:
    with TestClient(app) as client:
        create_response = client.post("/tenants", json={"display_name": "Slug Lookup Ltd"})
        tenant_id = create_response.json()["tenant_id"]
        lookup_response = client.get("/tenants/by-slug/slug-lookup-ltd")

    assert create_response.status_code == 201
    assert lookup_response.status_code == 200
    assert lookup_response.json()["tenant_id"] == tenant_id
    assert lookup_response.json()["slug"] == "slug-lookup-ltd"
    assert "config" not in lookup_response.json()


def test_get_tenant_returns_404_for_unknown_tenant() -> None:
    with TestClient(app) as client:
        response = client.get("/tenants/unknown-tenant")

    assert response.status_code == 404
    assert response.json()["detail"] == "Tenant not found"


def test_tenant_config_can_be_read_and_updated() -> None:
    with TestClient(app) as client:
        default_response = client.get("/tenants/tenant-a/config")
        update_response = client.put(
            "/tenants/tenant-a/config",
            json={
                "selected_plan": "enterprise",
                "enabled_features": ["telegram", "whatsapp"],
                "answer_prompt_instructions": "Use Tenant A's concise tone.",
                "planner_prompt_instructions": "Tenant A bookings are in scope.",
                "llm_project_id": "proj_tenant_a",
                "llm_project_name": "customer-service-tenant-a",
                "langsmith_project": "customer-service-tenant-a",
                "llm_provider": "langchain-compatible",
                "llm_model": "deepseek-chat",
                "llm_base_url": "https://api.deepseek.com",
                "vector_provider": "pgvector",
                "vector_isolation_mode": "shared_collection",
                "vector_collection": "customer-service",
                "vector_namespace": "tenant-a:seed-knowledge",
                "telegram_secret_name": "tenant-a-telegram",
                "whatsapp_secret_name": "tenant-a-whatsapp",
            },
        )
        get_response = client.get("/tenants/tenant-a/config")

    assert default_response.status_code == 404
    assert default_response.json()["detail"] == "Tenant config not found"
    assert update_response.status_code == 200
    assert update_response.json()["tenant_id"] == "tenant-a"
    assert update_response.json()["selected_plan"] == "enterprise"
    assert update_response.json()["enabled_features"] == [
        "telegram",
        "whatsapp",
    ]
    assert update_response.json()["answer_prompt_instructions"] == (
        "Use Tenant A's concise tone."
    )
    assert update_response.json()["planner_prompt_instructions"] == (
        "Tenant A bookings are in scope."
    )
    assert get_response.status_code == 200
    assert get_response.json()["answer_prompt_instructions"] == (
        "Use Tenant A's concise tone."
    )
    assert get_response.json()["llm_project_id"] == "proj_tenant_a"
    assert get_response.json()["llm_provider"] == "langchain-compatible"
    assert get_response.json()["llm_model"] == "deepseek-chat"
    assert get_response.json()["llm_base_url"] == "https://api.deepseek.com"
    assert get_response.json()["telegram_secret_name"] == "tenant-a-telegram"
    assert get_response.json()["whatsapp_secret_name"] == "tenant-a-whatsapp"


def test_tenant_config_rejects_unknown_feature() -> None:
    with TestClient(app) as client:
        response = client.put(
            "/tenants/tenant-a/config",
            json={"enabled_features": ["telegram", "unknown_feature"]},
        )

    assert response.status_code == 422
    assert any(
        "Input should be 'multimedia', 'telegram' or 'whatsapp'"
        in message
        for message in validation_messages(response)
    )


def test_tenant_config_rejects_unknown_providers() -> None:
    with TestClient(app) as client:
        llm_response = client.put(
            "/tenants/tenant-a/config",
            json={"llm_provider": "custom-provider"},
        )
        vector_response = client.put(
            "/tenants/tenant-a/config",
            json={"vector_provider": "custom-vector"},
        )

    assert llm_response.status_code == 422
    assert any(
        "Input should be 'langchain-compatible' or 'openai'" in message
        for message in validation_messages(llm_response)
    )
    assert vector_response.status_code == 422
    assert any(
        "Input should be 'pgvector', 'pinecone' or 'qdrant'" in message
        for message in validation_messages(vector_response)
    )


def test_tenant_config_rejects_invalid_llm_base_url() -> None:
    with TestClient(app) as client:
        response = client.put(
            "/tenants/tenant-a/config",
            json={"llm_base_url": "not-a-url"},
        )

    assert response.status_code == 422
    assert any(
        "llm_base_url must be an absolute http(s) URL" in message
        for message in validation_messages(response)
    )


def test_conversation_state_can_be_read_and_updated() -> None:
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
            f"/conversations/{conversation_id}/state",
            json={
                "state": "HUMAN_ACTIVE",
                "reason": "Human support accepted the handoff",
            },
        )

    assert webhook_response.status_code == 200
    assert webhook_response.json()["low_confidence"] is True
    assert webhook_response.json()["state"] == "BOT_ACTIVE"
    assert get_response.status_code == 200
    assert get_response.json()["state"] == "BOT_ACTIVE"
    assert update_response.status_code == 200
    assert update_response.json()["state"] == "HUMAN_ACTIVE"


def test_explicit_human_request_updates_conversation_state() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/messages/customer",
            json={
                "event_id": "api-human-request-1",
                "external_chat_id": "chat-human-request-1",
                "external_user_id": "user-human-request-1",
                "text": "Can I speak to a human agent?",
            },
        )

    assert response.status_code == 200
    assert response.json()["state"] == "HUMAN_REQUESTED"
    assert response.json()["low_confidence"] is True


def test_telegram_webhook_receives_customer_message_and_sends_reply() -> None:
    sender = FakeTelegramSender()
    with TestClient(app) as client:
        seed_default_knowledge(
            client,
            "refunds.txt",
            "Refund requests can be submitted within 30 days of purchase.",
        )
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
    assert response.json()["reply"]["citations"] == ["kb/refunds.txt#0000"]
    assert sender.sent_messages == [
        (
            "3003",
            "Refund requests can be submitted within 30 days of purchase.",
            "default",
        )
    ]


def test_telegram_webhook_uses_resolved_tenant_secret_token() -> None:
    resolver = FakeTelegramCredentialResolver(webhook_secret_token="tenant-secret")
    sender = FakeTelegramSender()

    with TestClient(app) as client:
        client.app.state.container.telegram_credentials = resolver
        client.app.state.container.telegram_sender = sender
        response = client.post(
            "/webhooks/telegram?tenant_id=tenant-a",
            headers={"X-Telegram-Bot-Api-Secret-Token": "tenant-secret"},
            json={
                "update_id": 1001,
                "message": {
                    "message_id": 2002,
                    "chat": {"id": 3003},
                    "from": {"id": 4004},
                    "text": "Unknown question?",
                },
            },
        )

    assert response.status_code == 200
    assert resolver.tenant_ids == ["tenant-a"]
    assert sender.sent_messages[0][2] == "tenant-a"


def test_telegram_webhook_rejects_invalid_resolved_secret_token() -> None:
    resolver = FakeTelegramCredentialResolver(webhook_secret_token="expected-secret")

    with TestClient(app) as client:
        client.app.state.container.telegram_credentials = resolver
        response = client.post(
            "/webhooks/telegram?tenant_id=tenant-a",
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


def test_whatsapp_webhook_verification_accepts_matching_verify_token() -> None:
    resolver = FakeWhatsAppCredentialResolver(verify_token="expected-token")

    with TestClient(app) as client:
        client.app.state.container.whatsapp_credentials = resolver
        response = client.get(
            "/webhooks/whatsapp",
            params={
                "tenant_id": "tenant-a",
                "hub.mode": "subscribe",
                "hub.challenge": "challenge-value",
                "hub.verify_token": "expected-token",
            },
        )

    assert response.status_code == 200
    assert response.text == "challenge-value"
    assert resolver.tenant_ids == ["tenant-a"]


def test_whatsapp_webhook_verification_rejects_invalid_verify_token() -> None:
    resolver = FakeWhatsAppCredentialResolver(verify_token="expected-token")

    with TestClient(app) as client:
        client.app.state.container.whatsapp_credentials = resolver
        response = client.get(
            "/webhooks/whatsapp",
            params={
                "tenant_id": "tenant-a",
                "hub.mode": "subscribe",
                "hub.challenge": "challenge-value",
                "hub.verify_token": "wrong-token",
            },
        )

    assert response.status_code == 403
    assert resolver.tenant_ids == ["tenant-a"]


def test_whatsapp_webhook_receives_customer_message_and_sends_reply() -> None:
    sender = FakeWhatsAppSender()
    with TestClient(app) as client:
        seed_default_knowledge(
            client,
            "refunds.txt",
            "Refund requests can be submitted within 30 days of purchase.",
        )
        client.app.state.container.whatsapp_sender = sender
        response = client.post(
            "/webhooks/whatsapp",
            json={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "contacts": [
                                        {
                                            "wa_id": "254700000001",
                                            "profile": {"name": "Ada"},
                                        }
                                    ],
                                    "messages": [
                                        {
                                            "from": "254700000001",
                                            "id": "wamid.1001",
                                            "type": "text",
                                            "text": {"body": "What is the refund policy?"},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["replies"][0]["citations"] == ["kb/refunds.txt#0000"]
    assert sender.sent_messages == [
        (
            "254700000001",
            "Refund requests can be submitted within 30 days of purchase.",
            "default",
        )
    ]


def test_whatsapp_webhook_ignores_non_text_updates() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/webhooks/whatsapp",
            json={
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "messages": [
                                        {
                                            "from": "254700000001",
                                            "id": "wamid.1001",
                                            "type": "image",
                                            "image": {"id": "media-id"},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "ignored": True}
