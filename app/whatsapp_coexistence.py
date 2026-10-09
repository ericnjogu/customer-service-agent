"""Coexistence events are archival input, never prompts for automatic replies."""

import re
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ImportedMessage:
    message_id: str
    chat_id: str
    sender_type: str
    body: str
    message_type: str
    sent_at: datetime
    is_history: bool


def imported_messages(value: dict, *, history: bool, incoming: bool = False):
    if history:
        batches = value.get("history") or []
        if not isinstance(batches, list):
            return
        groups = (
            (str(thread.get("id", "")), thread.get("messages", []))
            for batch in batches
            if isinstance(batch, dict)
            if isinstance(batch.get("threads"), list)
            for thread in batch["threads"]
            if isinstance(thread, dict)
        )
    else:
        groups = [(None, value.get("messages" if incoming else "message_echoes", []))]
    for thread_id, messages in groups:
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            chat = thread_id if history else str(message.get("from" if incoming else "to", ""))
            if not chat or not re.fullmatch(r"\d{1,32}", chat):
                continue
            sender = str(message.get("from", ""))
            outbound = not incoming and (not history or sender != chat)
            if history and outbound and str(message.get("to", "")) != chat:
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id or len(message_id) > 500:
                continue
            try:
                sent_at = datetime.fromtimestamp(int(message["timestamp"]), timezone.utc)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
            kind = message.get("type", "unknown")
            if not isinstance(kind, str):
                continue
            content = message.get(kind, {})
            text = content.get("body") if kind == "text" and isinstance(content, dict) else None
            # Retain media provenance without fetching untrusted URLs or inventing text.
            body = text if isinstance(text, str) else f"[WhatsApp {kind} message]"
            yield ImportedMessage(
                message_id, chat, "AGENT" if outbound else "CUSTOMER", body, kind, sent_at, history
            )


async def handle_coexistence(repository, waba_id: str, phone_id: str, field: str, value: dict):
    async with repository.delivery_lock(waba_id, phone_id) as sql, sql.transaction():
        connection = await sql.fetchrow(
            "SELECT * FROM whatsapp_connections WHERE waba_id=$1 AND phone_number_id=$2 "
            "AND coexistence FOR UPDATE",
            waba_id,
            phone_id,
        )
        if not connection:
            return
        key = connection["connection_id"]
        history = field == "history"
        if history and connection.get("import_history_enabled") is False:
            return
        incoming = field == "messages"
        if incoming:
            # Text keeps its existing graph path; only media needs archival persistence here.
            value = {
                **value,
                "messages": [
                    m
                    for m in value.get("messages", [])
                    if isinstance(m, dict)
                    and m.get("type") in {"image", "document", "audio", "video", "sticker"}
                ],
            }
        for message in imported_messages(value, history=history, incoming=incoming):
            inserted = await sql.fetchval(
                "INSERT INTO whatsapp_imported_messages(connection_id,message_id,chat_id, "
                "sender_type,body,message_type,sent_at,is_history) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING RETURNING message_id",
                key,
                message.message_id,
                message.chat_id,
                message.sender_type,
                message.body,
                message.message_type,
                message.sent_at,
                message.is_history,
            )
            if inserted and field == "smb_message_echoes" and connection["pause_on_human_reply"]:
                await sql.execute(
                    "INSERT INTO whatsapp_paused_chats(connection_id,chat_id) VALUES($1,$2) "
                    "ON CONFLICT DO NOTHING",
                    key,
                    message.chat_id,
                )
        if connection.get("archive_media_enabled"):
            from app.media_archive import enqueue_attachments

            raw = value.get("messages" if incoming else "message_echoes", [])
            if history:
                raw = [
                    m
                    for b in value.get("history", [])
                    if isinstance(b, dict)
                    for t in (b.get("threads") or [])
                    if isinstance(t, dict)
                    for m in (t.get("messages") or [])
                ]
            for message in raw if isinstance(raw, list) else []:
                if isinstance(message, dict) and await sql.fetchval(
                    "SELECT 1 FROM whatsapp_imported_messages "
                    "WHERE connection_id=$1 AND message_id=$2",
                    key,
                    str(message.get("id", "")),
                ):
                    await enqueue_attachments(sql, connection, message)
        if history:
            batches = value.get("history")
            for batch in batches if isinstance(batches, list) else []:
                if not isinstance(batch, dict):
                    continue
                errors = batch.get("errors") or []
                if not isinstance(errors, list):
                    errors = [{}]
                status = "receiving"
                if errors:
                    status = (
                        "declined"
                        if any(
                            isinstance(error, dict) and error.get("code") == 2593109
                            for error in errors
                        )
                        else "failed"
                    )
                await sql.execute(
                    "UPDATE whatsapp_connections SET history_status=$2 WHERE connection_id=$1",
                    key,
                    status,
                )
        if connection["tenant_id"]:
            await import_pending(sql, key, connection["tenant_id"])


async def import_pending(sql, connection_id, tenant_id):
    """Runs inside activation or webhook transaction; no LLM, queue, or outbound send."""
    records = await sql.fetch(
        "SELECT * FROM whatsapp_imported_messages WHERE connection_id=$1 "
        "AND imported_at IS NULL ORDER BY sent_at,message_id",
        connection_id,
    )
    for row in records:
        conversation_id = await sql.fetchval(
            "INSERT INTO conversations(tenant_id,channel,external_chat_id,external_user_id) "
            "VALUES($1,'whatsapp',$2,$2) ON CONFLICT(tenant_id,channel,external_chat_id) "
            "DO UPDATE SET external_chat_id=EXCLUDED.external_chat_id RETURNING id",
            tenant_id,
            row["chat_id"],
        )
        await sql.execute(
            "INSERT INTO messages(tenant_id,conversation_id,event_id,sender_type,body,created_at) "
            "VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(tenant_id,event_id) DO NOTHING",
            tenant_id,
            conversation_id,
            f"whatsapp:{row['message_id']}",
            row["sender_type"],
            row["body"],
            row["sent_at"],
        )
        await sql.execute(
            "UPDATE whatsapp_imported_messages SET imported_at=now() "
            "WHERE connection_id=$1 AND message_id=$2",
            connection_id,
            row["message_id"],
        )
    if await sql.fetchval("SELECT to_regclass('message_attachments')"):
        from app.media_archive import link_attachments

        await link_attachments(sql, connection_id, tenant_id)
        await sql.execute(
            "UPDATE drive_connections d SET tenant_id=$2 FROM whatsapp_connections c "
            "WHERE c.connection_id=$1 AND c.drive_connection_id=d.id",
            connection_id,
            tenant_id,
        )
