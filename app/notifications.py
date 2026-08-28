import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send_email(self, *, to: list[str], subject: str, text: str) -> None: ...


@dataclass
class SentEmail:
    to: list[str]
    subject: str
    text: str


@dataclass
class LoggingEmailSender:
    sent_messages: list[SentEmail] = field(default_factory=list)

    async def send_email(self, *, to: list[str], subject: str, text: str) -> None:
        message = SentEmail(to=to, subject=subject, text=text)
        self.sent_messages.append(message)
        logger.info("Email notification recorded subject=%s to=%s", subject, ",".join(to))


class ResendEmailSender:
    def __init__(self, *, api_key: str, from_email: str) -> None:
        self.api_key = api_key
        self.from_email = from_email

    async def send_email(self, *, to: list[str], subject: str, text: str) -> None:
        started_at = time.perf_counter()
        logger.info(
            "Sending email via Resend subject=%s from=%s to=%s timeout_seconds=10",
            subject,
            self.from_email,
            ",".join(to),
        )
        async with httpx.AsyncClient(timeout=10) as client:
            response: httpx.Response | None = None
            try:
                response = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "from": self.from_email,
                        "to": to,
                        "subject": subject,
                        "text": text,
                    },
                )
                response.raise_for_status()
            except httpx.HTTPStatusError:
                logger.exception(
                    "Failed to send email via Resend subject=%s from=%s to=%s "
                    "status_code=%s response_body=%s elapsed_seconds=%.3f",
                    subject,
                    self.from_email,
                    ",".join(to),
                    response.status_code if response is not None else None,
                    safe_response_text(response),
                    time.perf_counter() - started_at,
                )
                raise
            except Exception:
                logger.exception(
                    "Failed to send email via Resend subject=%s from=%s to=%s "
                    "elapsed_seconds=%.3f",
                    subject,
                    self.from_email,
                    ",".join(to),
                    time.perf_counter() - started_at,
                )
                raise
        logger.info(
            "Email sent via Resend subject=%s to=%s status_code=%s message_id=%s "
            "elapsed_seconds=%.3f",
            subject,
            ",".join(to),
            response.status_code,
            read_resend_message_id(response),
            time.perf_counter() - started_at,
        )


def read_resend_message_id(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        message_id = body.get("id")
        return str(message_id) if message_id else None
    return None


def safe_response_text(response: httpx.Response | None) -> str | None:
    if response is None:
        return None
    text = response.text.strip()
    if len(text) > 1_000:
        return f"{text[:1_000]}..."
    return text
