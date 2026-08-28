import httpx

from app.adapters.telegram import (
    KubernetesSecretTelegramSecretWriter,
    TelegramBotWebhookRegistrar,
    telegram_reply_text,
    telegram_update_to_incoming_message,
)
from app.models import ServiceReply


def test_telegram_update_to_incoming_message_maps_text_message() -> None:
    message = telegram_update_to_incoming_message(
        {
            "update_id": 123,
            "message": {
                "message_id": 456,
                "chat": {"id": 789},
                "from": {"id": 321, "first_name": "Ada", "last_name": "Lovelace"},
                "text": "Hello",
            },
        }
    )

    assert message is not None
    assert message.event_id == "telegram:123:456"
    assert message.channel == "telegram"
    assert message.external_chat_id == "789"
    assert message.external_user_id == "321"
    assert message.sender_name == "Ada Lovelace"
    assert message.text == "Hello"


def test_telegram_update_to_incoming_message_ignores_non_text_message() -> None:
    message = telegram_update_to_incoming_message(
        {
            "update_id": 123,
            "message": {
                "message_id": 456,
                "chat": {"id": 789},
                "from": {"id": 321},
                "photo": [{"file_id": "photo-id"}],
            },
        }
    )

    assert message is None


def test_telegram_reply_text_omits_low_confidence_human_follow_up() -> None:
    text = telegram_reply_text(
        ServiceReply(
            conversation_id="00000000-0000-0000-0000-000000000000",
            answer="I could not answer that.",
            confidence=0.0,
            citations=[],
            low_confidence=True,
            state="BOT_ACTIVE",
        )
    )

    assert text == "I could not answer that."


async def test_telegram_webhook_registrar_posts_expected_set_webhook_payload(
    monkeypatch,
) -> None:
    requests = []

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, json):
            requests.append((url, json, self.timeout))
            return httpx.Response(
                200,
                json={"ok": True},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.adapters.telegram.httpx.AsyncClient", FakeAsyncClient)
    registrar = TelegramBotWebhookRegistrar(
        public_base_url="https://onboarding.example.com/api/",
    )

    webhook_url = await registrar.register_webhook(
        tenant_id="tnt_123",
        bot_token="123456:telegram-token",
        webhook_secret_token="webhook-secret",
    )

    assert webhook_url == (
        "https://onboarding.example.com/api/webhooks/telegram?tenant_id=tnt_123"
    )
    assert requests == [
        (
            "https://api.telegram.org/bot123456:telegram-token/setWebhook",
            {
                "url": (
                    "https://onboarding.example.com/api/webhooks/telegram"
                    "?tenant_id=tnt_123"
                ),
                "secret_token": "webhook-secret",
                "allowed_updates": ["message"],
            },
            10.0,
        )
    ]


async def test_kubernetes_telegram_secret_writer_creates_expected_secret(
    monkeypatch,
    tmp_path,
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("service-account-token", encoding="utf-8")
    ca_path = tmp_path / "missing-ca"
    requests = []

    class FakeAsyncClient:
        def __init__(self, *, timeout, verify):
            self.timeout = timeout
            self.verify = verify

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, headers, json):
            requests.append(("post", url, headers, json, self.timeout, self.verify))
            return httpx.Response(
                201,
                json={"metadata": {"name": "tenant-hustle-hq-telegram"}},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.adapters.telegram.httpx.AsyncClient", FakeAsyncClient)
    writer = KubernetesSecretTelegramSecretWriter(
        namespace="customer-service",
        service_account_token_path=str(token_path),
        service_account_ca_path=str(ca_path),
    )

    await writer.write_secret(
        secret_name="tenant-hustle-hq-telegram",
        bot_token="123456:telegram-token",
        webhook_secret_token="webhook-secret",
    )

    assert requests == [
        (
            "post",
            "https://kubernetes.default.svc/api/v1/namespaces/customer-service/secrets",
            {"authorization": "Bearer service-account-token"},
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "tenant-hustle-hq-telegram"},
                "type": "Opaque",
                "stringData": {
                    "TELEGRAM_BOT_TOKEN": "123456:telegram-token",
                    "TELEGRAM_WEBHOOK_SECRET_TOKEN": "webhook-secret",
                },
            },
            10.0,
            True,
        )
    ]


async def test_kubernetes_telegram_secret_writer_patches_existing_secret(
    monkeypatch,
    tmp_path,
) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("service-account-token", encoding="utf-8")
    requests = []

    class FakeAsyncClient:
        def __init__(self, *, timeout, verify):
            self.timeout = timeout
            self.verify = verify

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, headers, json):
            requests.append(("post", url, headers, json))
            return httpx.Response(
                409,
                json={"reason": "AlreadyExists"},
                request=httpx.Request("POST", url),
            )

        async def patch(self, url, *, headers, json):
            requests.append(("patch", url, headers, json))
            return httpx.Response(
                200,
                json={"metadata": {"name": "tenant-hustle-hq-telegram"}},
                request=httpx.Request("PATCH", url),
            )

    monkeypatch.setattr("app.adapters.telegram.httpx.AsyncClient", FakeAsyncClient)
    writer = KubernetesSecretTelegramSecretWriter(
        namespace="customer-service",
        service_account_token_path=str(token_path),
        service_account_ca_path=str(tmp_path / "missing-ca"),
    )

    await writer.write_secret(
        secret_name="tenant-hustle-hq-telegram",
        bot_token="123456:telegram-token",
        webhook_secret_token="webhook-secret",
    )

    assert requests[-1] == (
        "patch",
        (
            "https://kubernetes.default.svc/api/v1/namespaces/customer-service/"
            "secrets/tenant-hustle-hq-telegram"
        ),
        {
            "authorization": "Bearer service-account-token",
            "content-type": "application/merge-patch+json",
        },
        {
            "stringData": {
                "TELEGRAM_BOT_TOKEN": "123456:telegram-token",
                "TELEGRAM_WEBHOOK_SECRET_TOKEN": "webhook-secret",
            }
        },
    )
