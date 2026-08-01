import base64
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from app.models import IncomingMessage, ServiceReply
from app.ports import TenantConfigRepository

logger = logging.getLogger(__name__)


class WhatsAppSender(Protocol):
    async def send_message(
        self,
        to: str,
        text: str,
        tenant_id: str = "default",
    ) -> None: ...


@dataclass(frozen=True)
class WhatsAppCredentials:
    access_token: str | None = None
    phone_number_id: str | None = None
    verify_token: str | None = None
    graph_api_version: str = "v20.0"


class WhatsAppCredentialResolver(Protocol):
    async def resolve(self, tenant_id: str) -> WhatsAppCredentials: ...


class WhatsAppCloudClient:
    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        graph_api_version: str = "v20.0",
    ) -> None:
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.graph_api_version = graph_api_version

    async def send_message(self, to: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                (
                    f"https://graph.facebook.com/{self.graph_api_version}/"
                    f"{self.phone_number_id}/messages"
                ),
                headers={"Authorization": f"Bearer {self.access_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            response.raise_for_status()


class KubernetesSecretWhatsAppCredentialResolver:
    def __init__(
        self,
        *,
        tenant_configs: TenantConfigRepository,
        namespace: str | None = None,
        access_token_key: str = "WHATSAPP_ACCESS_TOKEN",
        phone_number_id_key: str = "WHATSAPP_PHONE_NUMBER_ID",
        verify_token_key: str = "WHATSAPP_VERIFY_TOKEN",
        graph_api_version_key: str = "WHATSAPP_GRAPH_API_VERSION",
        default_graph_api_version: str = "v20.0",
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
        self.namespace = namespace or read_optional_text(namespace_path)
        self.access_token_key = access_token_key
        self.phone_number_id_key = phone_number_id_key
        self.verify_token_key = verify_token_key
        self.graph_api_version_key = graph_api_version_key
        self.default_graph_api_version = default_graph_api_version
        self.api_server = api_server.rstrip("/")
        self.service_account_token_path = service_account_token_path
        self.service_account_ca_path = service_account_ca_path
        self.cache: dict[str, WhatsAppCredentials] = {}

    async def resolve(self, tenant_id: str) -> WhatsAppCredentials:
        tenant_config = await self.tenant_configs.get(tenant_id)
        secret_name = tenant_config.whatsapp_secret_name
        if not secret_name:
            logger.info(
                "Skipping WhatsApp credential lookup because tenant_id=%s has no "
                "whatsapp_secret_name configured",
                tenant_id,
            )
            return WhatsAppCredentials(graph_api_version=self.default_graph_api_version)

        if secret_name in self.cache:
            return self.cache[secret_name]

        credentials = await self._read_secret(secret_name)
        self.cache[secret_name] = credentials
        return credentials

    async def _read_secret(self, secret_name: str) -> WhatsAppCredentials:
        if not self.namespace:
            raise RuntimeError("Kubernetes namespace is required to read WhatsApp Secret")

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

        credentials = WhatsAppCredentials(
            access_token=decode_secret_value(data, self.access_token_key),
            phone_number_id=decode_secret_value(data, self.phone_number_id_key),
            verify_token=decode_secret_value(data, self.verify_token_key),
            graph_api_version=(
                decode_secret_value(data, self.graph_api_version_key)
                or self.default_graph_api_version
            ),
        )
        if not credentials.access_token:
            raise RuntimeError(
                f"WhatsApp Secret {secret_name!r} does not contain "
                f"{self.access_token_key!r}"
            )
        if not credentials.phone_number_id:
            raise RuntimeError(
                f"WhatsApp Secret {secret_name!r} does not contain "
                f"{self.phone_number_id_key!r}"
            )
        logger.info("Loaded tenant WhatsApp credentials from Secret %s", secret_name)
        return credentials


class TenantAwareWhatsAppSender:
    def __init__(self, credentials: WhatsAppCredentialResolver) -> None:
        self.credentials = credentials
        self.clients: dict[tuple[str, str, str], WhatsAppCloudClient] = {}

    async def send_message(
        self,
        to: str,
        text: str,
        tenant_id: str = "default",
    ) -> None:
        credentials = await self.credentials.resolve(tenant_id)
        if not credentials.access_token or not credentials.phone_number_id:
            logger.info("Skipping WhatsApp reply because no credentials are configured")
            return

        cache_key = (
            credentials.access_token,
            credentials.phone_number_id,
            credentials.graph_api_version,
        )
        client = self.clients.get(cache_key)
        if client is None:
            client = WhatsAppCloudClient(
                credentials.access_token,
                credentials.phone_number_id,
                credentials.graph_api_version,
            )
            self.clients[cache_key] = client
        await client.send_message(to, text)


def whatsapp_update_to_incoming_messages(update: dict) -> list[IncomingMessage]:
    messages: list[IncomingMessage] = []

    for entry in update.get("entry", []):
        if not isinstance(entry, dict):
            continue

        for change in entry.get("changes", []):
            if not isinstance(change, dict):
                continue

            value = change.get("value")
            if not isinstance(value, dict):
                continue

            contact_names = whatsapp_contact_names_by_id(value)
            for message in value.get("messages", []):
                incoming = whatsapp_message_to_incoming_message(
                    message,
                    contact_names=contact_names,
                )
                if incoming:
                    messages.append(incoming)

    return messages


def whatsapp_message_to_incoming_message(
    message: dict,
    *,
    contact_names: dict[str, str],
) -> IncomingMessage | None:
    if not isinstance(message, dict):
        return None

    from_id = message.get("from")
    message_id = message.get("id")
    if from_id is None or message_id is None:
        return None

    text = message.get("text")
    if not isinstance(text, dict):
        return None

    body = text.get("body")
    if not isinstance(body, str) or not body.strip():
        return None

    external_id = str(from_id)
    return IncomingMessage(
        event_id=f"whatsapp:{message_id}",
        channel="whatsapp",
        external_chat_id=external_id,
        external_user_id=external_id,
        sender_name=contact_names.get(external_id),
        text=body,
    )


def whatsapp_contact_names_by_id(value: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    for contact in value.get("contacts", []):
        if not isinstance(contact, dict):
            continue

        wa_id = contact.get("wa_id")
        profile = contact.get("profile")
        if wa_id is None or not isinstance(profile, dict):
            continue

        name = profile.get("name")
        if isinstance(name, str) and name.strip():
            names[str(wa_id)] = name.strip()

    return names


def whatsapp_reply_text(reply: ServiceReply) -> str:
    return reply.answer


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
