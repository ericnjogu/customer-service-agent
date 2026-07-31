import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from app.models import IncomingMessage, ServiceReply
from app.ports import TenantConfigRepository

logger = logging.getLogger(__name__)


class TelegramSender(Protocol):
    async def send_message(
        self,
        chat_id: str,
        text: str,
        tenant_id: str = "default",
    ) -> None: ...


@dataclass(frozen=True)
class TelegramCredentials:
    bot_token: str | None = None
    webhook_secret_token: str | None = None


class TelegramCredentialResolver(Protocol):
    async def resolve(self, tenant_id: str) -> TelegramCredentials: ...


class TelegramBotClient:
    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token

    async def send_message(self, chat_id: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
            response.raise_for_status()


class StaticTelegramCredentialResolver:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        webhook_secret_token: str | None = None,
    ) -> None:
        self.credentials = TelegramCredentials(
            bot_token=bot_token,
            webhook_secret_token=webhook_secret_token,
        )

    async def resolve(self, tenant_id: str) -> TelegramCredentials:
        return self.credentials


class KubernetesSecretTelegramCredentialResolver:
    def __init__(
        self,
        *,
        tenant_configs: TenantConfigRepository,
        fallback: TelegramCredentialResolver,
        namespace: str | None = None,
        bot_token_key: str = "TELEGRAM_BOT_TOKEN",
        webhook_secret_token_key: str = "TELEGRAM_WEBHOOK_SECRET_TOKEN",
        api_server: str = "https://kubernetes.default.svc",
        service_account_token_path: str = (
            "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ),
        service_account_ca_path: str = (
            "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        ),
        namespace_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/namespace",
    ) -> None:
        self.tenant_configs = tenant_configs
        self.fallback = fallback
        self.namespace = namespace or read_optional_text(namespace_path)
        self.bot_token_key = bot_token_key
        self.webhook_secret_token_key = webhook_secret_token_key
        self.api_server = api_server.rstrip("/")
        self.service_account_token_path = service_account_token_path
        self.service_account_ca_path = service_account_ca_path
        self.cache: dict[str, TelegramCredentials] = {}

    async def resolve(self, tenant_id: str) -> TelegramCredentials:
        tenant_config = await self.tenant_configs.get(tenant_id)
        secret_name = tenant_config.telegram_secret_name
        if not secret_name:
            return await self.fallback.resolve(tenant_id)

        if secret_name in self.cache:
            return self.cache[secret_name]

        credentials = await self._read_secret(secret_name)
        self.cache[secret_name] = credentials
        return credentials

    async def _read_secret(self, secret_name: str) -> TelegramCredentials:
        if not self.namespace:
            raise RuntimeError("Kubernetes namespace is required to read Telegram Secret")

        token = read_optional_text(self.service_account_token_path)
        if not token:
            raise RuntimeError("Kubernetes service account token is required to read Secret")

        verify: str | bool = (
            self.service_account_ca_path
            if Path(self.service_account_ca_path).exists()
            else True
        )
        url = (
            f"{self.api_server}/api/v1/namespaces/{self.namespace}/secrets/"
            f"{secret_name}"
        )
        async with httpx.AsyncClient(timeout=10.0, verify=verify) as client:
            response = await client.get(
                url,
                headers={"authorization": f"Bearer {token}"},
            )
            response.raise_for_status()

        data = response.json().get("data", {})
        if not isinstance(data, dict):
            data = {}

        credentials = TelegramCredentials(
            bot_token=decode_secret_value(data, self.bot_token_key),
            webhook_secret_token=decode_secret_value(
                data,
                self.webhook_secret_token_key,
            ),
        )
        if not credentials.bot_token:
            raise RuntimeError(
                f"Telegram Secret {secret_name!r} does not contain "
                f"{self.bot_token_key!r}"
            )
        logger.info("Loaded tenant Telegram credentials from Secret %s", secret_name)
        return credentials


class TenantAwareTelegramSender:
    def __init__(self, credentials: TelegramCredentialResolver) -> None:
        self.credentials = credentials
        self.clients: dict[str, TelegramBotClient] = {}

    async def send_message(self, chat_id: str, text: str, tenant_id: str = "default") -> None:
        credentials = await self.credentials.resolve(tenant_id)
        if not credentials.bot_token:
            logger.info("Skipping Telegram reply because no bot token is configured")
            return
        client = self.clients.get(credentials.bot_token)
        if client is None:
            client = TelegramBotClient(credentials.bot_token)
            self.clients[credentials.bot_token] = client
        await client.send_message(chat_id, text)


def read_optional_text(path: str) -> str | None:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def decode_secret_value(data: dict, key: str) -> str | None:
    value = data.get(key)
    if not isinstance(value, str):
        return None
    try:
        decoded = base64.b64decode(value).decode("utf-8").strip()
    except Exception:
        return None
    return decoded or None


def telegram_update_to_incoming_message(update: dict) -> IncomingMessage | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("id") is None:
        return None

    sender = message.get("from")
    if not isinstance(sender, dict) or sender.get("id") is None:
        return None

    update_id = str(update.get("update_id", "unknown-update"))
    message_id = str(message.get("message_id", "unknown-message"))
    sender_name = format_sender_name(sender)

    return IncomingMessage(
        event_id=f"telegram:{update_id}:{message_id}",
        channel="telegram",
        external_chat_id=str(chat["id"]),
        external_user_id=str(sender["id"]),
        sender_name=sender_name,
        text=text,
    )


def format_sender_name(sender: dict) -> str | None:
    names = [
        str(sender.get("first_name", "")).strip(),
        str(sender.get("last_name", "")).strip(),
    ]
    full_name = " ".join(name for name in names if name)
    if full_name:
        return full_name

    username = sender.get("username")
    return str(username) if username else None


def telegram_reply_text(reply: ServiceReply) -> str:
    return reply.answer
