"""Durable opt-in media archive. No extraction or answer-generation dependencies."""

import asyncio
import hashlib
import json
import re
import secrets
import tempfile
from datetime import timedelta
from functools import partial
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pgqueuer import (
    AsyncpgDriver,
    AsyncpgPoolDriver,
    DatabaseRetryEntrypointExecutor,
    Queries,
    QueueManager,
)

from app.adapters.google_drive import ArchiveError
from app.adapters.secret_transport import secret_transport
from app.whatsapp_signup import capability_hash

ENTRYPOINT = "whatsapp_media_archive"
MEDIA_TYPES = {"image", "document", "audio", "video", "sticker"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS drive_connections (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 session_id uuid NOT NULL UNIQUE REFERENCES onboarding_sessions(session_id),
 tenant_id text UNIQUE REFERENCES tenants(tenant_id),
 credential_reference text NOT NULL UNIQUE,
 account_id text,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN
 ('pending','ready','attention','disconnected')),
 error_code text,
 root_name text NOT NULL,
 root_id text, whatsapp_id text, conversations_id text,
 kb_id text, sources_id text, generated_id text, chunks_id text,
 folders_created boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS whatsapp_archive_preferences (
 session_id uuid PRIMARY KEY REFERENCES onboarding_sessions(session_id),
 import_enabled boolean NOT NULL DEFAULT false,
 locked boolean NOT NULL DEFAULT false,
 drive_id uuid REFERENCES drive_connections(id)
);
ALTER TABLE drive_connections
 ADD COLUMN IF NOT EXISTS folders_created boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS drive_oauth_attempts (
 state_hash text PRIMARY KEY,
 session_id uuid NOT NULL REFERENCES onboarding_sessions(session_id),
 browser_hash text NOT NULL, expires_at timestamptz NOT NULL,
 consumed_at timestamptz
);
ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS import_history_enabled boolean;
ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS archive_media_enabled
 boolean NOT NULL DEFAULT false;
ALTER TABLE whatsapp_connections ADD COLUMN IF NOT EXISTS drive_connection_id uuid
 REFERENCES drive_connections(id);
CREATE TABLE IF NOT EXISTS whatsapp_archive_chats (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 connection_id uuid NOT NULL REFERENCES whatsapp_connections(connection_id),
 chat_id text NOT NULL, folder_id text, media_folder_id text,
 folders_created boolean NOT NULL DEFAULT false,
 UNIQUE(connection_id,chat_id)
);
CREATE TABLE IF NOT EXISTS message_attachments (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 connection_id uuid NOT NULL,
 source_message_id text NOT NULL,
 media_id text NOT NULL,
 tenant_id text REFERENCES tenants(tenant_id),
 conversation_id uuid REFERENCES conversations(id), message_id uuid REFERENCES messages(id),
 media_type text NOT NULL, original_filename text, mime_type text, byte_size bigint, checksum text,
 drive_file_id text, upload_url text,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN
 ('pending','transferring','complete','failed')),
 error_code text, queue_id bigint,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(connection_id,source_message_id,media_id),
 FOREIGN KEY(connection_id,source_message_id) REFERENCES
 whatsapp_imported_messages(connection_id,message_id)
);
ALTER TABLE whatsapp_archive_chats
 ADD COLUMN IF NOT EXISTS folders_created boolean NOT NULL DEFAULT false;
"""


async def enqueue_attachments(sql, connection, message):
    if not connection.get("archive_media_enabled"):
        return
    kind = message.get("type")
    media = message.get(kind) if kind in MEDIA_TYPES else None
    if not isinstance(media, dict):
        return
    media_id = str(media.get("id", ""))
    # Missing historical media is visible, not silently dropped.
    available = bool(re.fullmatch(r"[0-9]{1,128}", media_id))
    media_id = media_id if available else "unavailable"
    attachment = await sql.fetchval(
        "INSERT INTO message_attachments(connection_id,source_message_id,media_id,media_type, "
        "original_filename,status,error_code) VALUES($1,$2,$3,$4,$5,$6,$7) "
        "ON CONFLICT DO NOTHING RETURNING id",
        connection["connection_id"],
        message["id"],
        media_id,
        kind,
        str(media.get("filename", ""))[:255] or None,
        "pending" if available else "failed",
        None if available else "media_unavailable",
    )
    if attachment and available:
        ids = await Queries(AsyncpgDriver(sql)).enqueue(
            ENTRYPOINT, json.dumps({"id": str(attachment)}).encode()
        )
        await sql.execute(
            "UPDATE message_attachments SET queue_id=$2 WHERE id=$1", attachment, ids[0]
        )


async def link_attachments(sql, connection_id, tenant_id):
    await sql.execute(
        "UPDATE message_attachments a SET "
        "tenant_id=$2,message_id=m.id,conversation_id=m.conversation_id "
        "FROM messages m WHERE a.connection_id=$1 AND m.tenant_id=$2 "
        "AND m.event_id='whatsapp:' || a.source_message_id",
        connection_id,
        tenant_id,
    )


class MediaArchive:
    def __init__(self, pool, vault, google, settings, meta_client):
        self.pool, self.vault, self.google = pool, vault, google
        self.settings, self.meta_client = settings, meta_client
        self.driver = AsyncpgPoolDriver(pool)
        self.manager = QueueManager(Queries(self.driver))
        self.manager.entrypoint(
            ENTRYPOINT,
            concurrency_limit=1,
            on_failure="hold",
            executor_factory=partial(
                DatabaseRetryEntrypointExecutor, max_attempts=4, initial_delay=timedelta(seconds=5)
            ),
        )(self.process)
        self.task = None

    async def initialize(self):
        async with self.pool.acquire() as sql, sql.transaction():
            await sql.execute("SELECT pg_advisory_xact_lock(78231650)")
            await sql.execute(SCHEMA)
            if not await sql.fetchval("SELECT to_regclass('pgqueuer')"):
                await Queries(AsyncpgDriver(sql)).install()

    async def start(self):
        self.task = asyncio.create_task(
            self.manager.run(batch_size=1, max_concurrent_tasks=2), name="media-archive-pgqueuer"
        )

    async def close(self):
        self.manager.shutdown.set()
        try:
            if self.task:
                try:
                    await asyncio.wait_for(self.task, timeout=10)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.driver.__aexit__(None, None, None)

    async def preferences(self, session_id, enabled):
        if enabled and not self.google:
            raise ArchiveError("drive_not_configured")
        row = await self.pool.fetchrow(
            "INSERT INTO whatsapp_archive_preferences(session_id,import_enabled) VALUES($1,$2) "
            "ON CONFLICT(session_id) DO UPDATE SET import_enabled=EXCLUDED.import_enabled "
            "WHERE NOT whatsapp_archive_preferences.locked RETURNING session_id",
            session_id,
            enabled,
        )
        if not row:
            raise ArchiveError("archive_preferences_locked")

    async def status(self, session_id):
        pref = await self.pool.fetchrow(
            "SELECT * FROM whatsapp_archive_preferences WHERE session_id=$1", session_id
        )
        drive = await self.pool.fetchrow(
            "SELECT * FROM drive_connections WHERE session_id=$1", session_id
        )
        counts = await self.pool.fetch(
            "SELECT a.status,count(*) AS count FROM "
            "message_attachments a JOIN whatsapp_connections c "
            "ON c.connection_id=a.connection_id WHERE c.session_id=$1 GROUP BY a.status",
            session_id,
        )
        history_status = await self.pool.fetchval(
            "SELECT history_status FROM whatsapp_connections WHERE session_id=$1", session_id
        )
        return {
            "history_status": history_status or "not_requested",
            "available": self.google is not None,
            "import_enabled": bool(pref and pref["import_enabled"]),
            "locked": bool(pref and pref["locked"]),
            "drive_status": drive["status"] if drive else "not_connected",
            "folder_name": drive["root_name"] if drive and drive["status"] == "ready" else None,
            "folder_url": f"https://drive.google.com/drive/folders/{drive['root_id']}"
            if drive and drive["status"] == "ready"
            else None,
            "error_code": drive["error_code"] if drive else None,
            "transfers": {r["status"]: r["count"] for r in counts},
        }

    async def authorize(self, session_id, browser):
        if not self.google:
            raise ArchiveError("drive_not_configured")
        state = secrets.token_urlsafe(32)
        async with self.pool.acquire() as sql, sql.transaction():
            await sql.execute(
                "UPDATE drive_oauth_attempts SET consumed_at=now() WHERE session_id=$1", session_id
            )
            await sql.execute(
                "INSERT INTO drive_oauth_attempts "
                "VALUES($1,$2,$3,now()+interval '10 minutes',NULL)",
                capability_hash(state),
                session_id,
                capability_hash(browser),
            )
        return self.google.authorization_url(state)

    async def complete(self, session_id, browser, state, code):
        if not self.google:
            raise ArchiveError("drive_not_configured")
        async with self.pool.acquire() as sql:
            await sql.execute(
                "SELECT pg_advisory_lock(hashtextextended($1,0))", f"drive:{session_id}"
            )
            try:
                valid = await sql.fetchval(
                    "UPDATE drive_oauth_attempts SET "
                    "consumed_at=now() WHERE state_hash=$1 AND session_id=$2 "
                    "AND browser_hash=$3 AND expires_at>now() AND "
                    "consumed_at IS NULL RETURNING session_id",
                    capability_hash(state),
                    session_id,
                    capability_hash(browser),
                )
                if not valid:
                    raise ArchiveError("drive_attempt_invalid")
                token, refresh = await self.google.exchange(code)
                account = await self.google.account(token)
                existing = await sql.fetchrow(
                    "SELECT * FROM drive_connections WHERE session_id=$1", session_id
                )
                if existing and existing["account_id"] and existing["account_id"] != account:
                    raise ArchiveError("drive_account_mismatch")
                key = existing["id"] if existing else uuid4()
                await self.vault.write_drive_token(key, refresh)
                payload = await sql.fetchval(
                    "SELECT session_payload FROM onboarding_sessions WHERE session_id=$1",
                    session_id,
                )
                data = json.loads(payload) if isinstance(payload, str) else payload or {}
                name = str((data.get("business_profile") or {}).get("business_name") or "Business")[
                    :80
                ]
                await sql.execute(
                    "INSERT INTO "
                    "drive_connections(id,session_id,credential_reference,root_name,account_id) "
                    "VALUES($1,$2,$3,$4,$5) ON "
                    "CONFLICT(session_id) DO UPDATE SET status='pending',error_code=NULL",
                    key,
                    session_id,
                    f"google-drive/{key}",
                    f"Ristoh CSS - {name} - {str(key)[:8]}",
                    account,
                )
                row = dict(await sql.fetchrow("SELECT * FROM drive_connections WHERE id=$1", key))
                for column, folder_name, parent in [
                    ("root_id", row["root_name"], None),
                    ("whatsapp_id", "WhatsApp", "root_id"),
                    ("conversations_id", "Conversations", "whatsapp_id"),
                    ("kb_id", "Knowledge Base", "root_id"),
                    ("sources_id", "Sources", "kb_id"),
                    ("generated_id", "Generated", "kb_id"),
                    ("chunks_id", "Chunks", "generated_id"),
                ]:
                    if not row[column]:
                        row[column] = await self.google.generate_id(token)
                        await sql.execute(
                            f"UPDATE drive_connections SET {column}=$2 WHERE id=$1",
                            key,
                            row[column],
                        )
                    if row["folders_created"]:
                        await self.google.private(token, row[column])
                    else:
                        await self.google.folder(
                            token, row[column], folder_name, row[parent] if parent else None
                        )
                await sql.execute(
                    "UPDATE drive_connections SET folders_created=true, "
                    "status='ready',error_code=NULL,updated_at=now() WHERE id=$1",
                    key,
                )
                await sql.execute(
                    "UPDATE whatsapp_archive_preferences SET drive_id=$2 WHERE session_id=$1",
                    session_id,
                    key,
                )
            finally:
                await sql.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1,0))", f"drive:{session_id}"
                )

    async def disconnect(self, session_id):
        await self.pool.execute(
            "UPDATE drive_connections SET "
            "status='disconnected',updated_at=now() WHERE session_id=$1",
            session_id,
        )

    async def replay_failed(self, session_id):
        async with self.pool.acquire() as sql, sql.transaction():
            records = await sql.fetch(
                "SELECT a.id,a.queue_id FROM message_attachments a "
                "JOIN whatsapp_connections c ON c.connection_id=a.connection_id "
                "JOIN drive_connections d ON d.id=c.drive_connection_id "
                "WHERE c.session_id=$1 AND d.status='ready' AND a.status='failed' "
                "AND a.media_id <> 'unavailable' ORDER BY a.created_at LIMIT 100 "
                "FOR UPDATE OF a",
                session_id,
            )
            queries = Queries(AsyncpgDriver(sql))
            for row in records:
                queued = await sql.fetchval(
                    "SELECT status FROM pgqueuer WHERE id=$1", row["queue_id"]
                )
                if queued and queued != "failed":
                    continue
                if queued:
                    await queries.requeue_jobs([row["queue_id"]])
                    queue_id = row["queue_id"]
                else:
                    queue_id = (
                        await queries.enqueue(
                            ENTRYPOINT, json.dumps({"id": str(row["id"])}).encode()
                        )
                    )[0]
                await sql.execute(
                    "UPDATE message_attachments SET status='pending',error_code=NULL,queue_id=$2 "
                    "WHERE id=$1",
                    row["id"],
                    queue_id,
                )

    async def process(self, job):
        attachment_id = UUID(json.loads(job.payload)["id"])
        async with self.pool.acquire() as sql:
            await sql.execute("SELECT pg_advisory_lock(hashtextextended($1,0))", str(attachment_id))
            try:
                row = await sql.fetchrow(
                    "SELECT a.*,m.chat_id,c.drive_connection_id FROM message_attachments a "
                    "JOIN whatsapp_imported_messages m ON "
                    "(m.connection_id,m.message_id)=(a.connection_id,a.source_message_id) "
                    "JOIN whatsapp_connections c ON c.connection_id=a.connection_id WHERE a.id=$1",
                    attachment_id,
                )
                if not row or row["status"] == "complete":
                    return
                try:
                    await self.transfer(sql, row)
                except ArchiveError as error:
                    await sql.execute(
                        "UPDATE message_attachments SET "
                        "status='failed',error_code=$2,updated_at=now() WHERE id=$1",
                        attachment_id,
                        error.code,
                    )
                    if error.code in {
                        "drive_reconnect_required",
                        "drive_quota_exceeded",
                        "drive_folder_not_private",
                    }:
                        await sql.execute(
                            "UPDATE drive_connections SET "
                            "status='attention',error_code=$2 WHERE id=$1",
                            row["drive_connection_id"],
                            error.code,
                        )
                    if error.retryable:
                        raise
                except Exception:
                    await sql.execute(
                        "UPDATE message_attachments SET "
                        "status='failed',error_code='transfer_failed' WHERE id=$1",
                        attachment_id,
                    )
                    raise ArchiveError("transfer_failed", retryable=True) from None
            finally:
                await sql.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1,0))", str(attachment_id)
                )

    async def transfer(self, sql, attachment):
        if not self.google:
            raise ArchiveError("drive_not_configured")
        drive = await sql.fetchrow(
            "SELECT * FROM drive_connections WHERE id=$1", attachment["drive_connection_id"]
        )
        if not drive or drive["status"] != "ready":
            raise ArchiveError("drive_reconnect_required")
        token = await self.google.refresh(await self.vault.read_drive_token(drive["id"]))
        await self.google.private(token, drive["root_id"])
        await sql.execute(
            "UPDATE message_attachments SET status='transferring',error_code=NULL WHERE id=$1",
            attachment["id"],
        )
        # One folder per chat, serialized across replicas. Persist IDs before remote writes.
        async with sql.transaction():
            await sql.execute(
                "INSERT INTO "
                "whatsapp_archive_chats(connection_id,chat_id) VALUES($1,$2) "
                "ON CONFLICT DO NOTHING",
                attachment["connection_id"],
                attachment["chat_id"],
            )
            chat = dict(
                await sql.fetchrow(
                    "SELECT * FROM whatsapp_archive_chats WHERE "
                    "connection_id=$1 AND chat_id=$2 FOR UPDATE",
                    attachment["connection_id"],
                    attachment["chat_id"],
                )
            )
            for column in ("folder_id", "media_folder_id"):
                if not chat[column]:
                    chat[column] = await self.google.generate_id(token)
                    await sql.execute(
                        f"UPDATE whatsapp_archive_chats SET {column}=$2 WHERE id=$1",
                        chat["id"],
                        chat[column],
                    )
        if chat["folders_created"]:
            await self.google.private(token, chat["folder_id"])
            await self.google.private(token, chat["media_folder_id"])
        else:
            await self.google.folder(
                token, chat["folder_id"], str(chat["id"]), drive["conversations_id"]
            )
            await self.google.folder(token, chat["media_folder_id"], "Media", chat["folder_id"])
            await sql.execute(
                "UPDATE whatsapp_archive_chats SET folders_created=true WHERE id=$1", chat["id"]
            )
        file_id = attachment["drive_file_id"]
        if not file_id:
            file_id = await self.google.generate_id(token)
            await sql.execute(
                "UPDATE message_attachments SET drive_file_id=$2 WHERE id=$1",
                attachment["id"],
                file_id,
            )
        else:
            try:
                metadata = await self.google.metadata(token, file_id)
                if metadata.get("trashed"):
                    raise ArchiveError("drive_file_deleted")
                if (
                    metadata.get("md5Checksum")
                    and attachment["checksum"] == metadata["md5Checksum"]
                ):
                    await self.google.private(token, file_id)
                    await sql.execute(
                        "UPDATE message_attachments SET "
                        "status='complete',upload_url=NULL,error_code=NULL WHERE id=$1",
                        attachment["id"],
                    )
                    return
            except ArchiveError as error:
                if error.code != "drive_file_unavailable":
                    raise
        credentials = await self.vault.read(attachment["connection_id"])
        with tempfile.TemporaryFile() as file:
            size, mime, checksum, extension = await self.download(
                credentials.access_token, attachment["media_id"], file
            )
            await sql.execute(
                "UPDATE message_attachments SET byte_size=$2,mime_type=$3,checksum=$4 WHERE id=$1",
                attachment["id"],
                size,
                mime,
                checksum,
            )
            if (
                await sql.fetchval("SELECT status FROM drive_connections WHERE id=$1", drive["id"])
                != "ready"
            ):
                raise ArchiveError("drive_reconnect_required")
            url = attachment["upload_url"]
            if not url:
                url = await self.google.begin_upload(
                    token,
                    file_id,
                    chat["media_folder_id"],
                    f"{attachment['id']}{extension}",
                    mime,
                    size,
                )
                await sql.execute(
                    "UPDATE message_attachments SET upload_url=$2 WHERE id=$1",
                    attachment["id"],
                    url,
                )
            try:
                await self.google.upload(token, url, file, size, mime)
            except ArchiveError as error:
                if error.code == "drive_file_unavailable":
                    await sql.execute(
                        "UPDATE message_attachments SET upload_url=NULL WHERE id=$1",
                        attachment["id"],
                    )
                    raise ArchiveError("upload_expired", retryable=True) from None
                raise
        await self.google.private(token, file_id)
        completed = await self.google.metadata(token, file_id)
        if completed.get("md5Checksum") != checksum:
            raise ArchiveError("upload_integrity_failed")
        await sql.execute(
            "UPDATE message_attachments SET "
            "status='complete',upload_url=NULL,error_code=NULL,updated_at=now() WHERE id=$1",
            attachment["id"],
        )

    async def download(self, token, media_id, file):
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        try:
            with secret_transport():
                response = await self.meta_client.get(
                    f"/{self.settings.meta_graph_api_version}/{media_id}", headers=headers
                )
                if response.status_code in (400, 404, 410):
                    raise ArchiveError("media_unavailable")
                response.raise_for_status()
                data = response.json()
                url = data.get("url", "")
                host = urlsplit(url)
                if (
                    host.scheme != "https"
                    or host.hostname != "lookaside.fbsbx.com"
                    or host.port not in (None, 443)
                    or host.username
                ):
                    raise ArchiveError("invalid_media_url")
                if int(data.get("file_size", 0)) > self.settings.media_archive_max_bytes:
                    raise ArchiveError("media_too_large")
                async with self.meta_client.stream(
                    "GET", url, headers=headers, follow_redirects=False
                ) as download:
                    download.raise_for_status()
                    mime = data.get("mime_type", "").split(";", 1)[0].lower()
                    if mime not in MIME_EXTENSIONS:
                        raise ArchiveError("unsupported_media_type")
                    size, digest, prefix = 0, hashlib.md5(), b""
                    async for chunk in download.aiter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > self.settings.media_archive_max_bytes:
                            raise ArchiveError("media_too_large")
                        prefix = (prefix + chunk)[:512]
                        digest.update(chunk)
                        file.write(chunk)
                if not size or not valid_signature(mime, prefix):
                    raise ArchiveError("media_type_mismatch")
                file.seek(0)
                return size, mime, digest.hexdigest(), MIME_EXTENSIONS[mime]
        except httpx.HTTPError:
            raise ArchiveError("media_download_failed", retryable=True) from None


MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


def valid_signature(mime, data):
    if mime == "text/plain":
        return b"\x00" not in data and not data.startswith((b"MZ", b"\x7fELF"))
    signatures = {
        "image/jpeg": b"\xff\xd8\xff",
        "image/png": b"\x89PNG\r\n\x1a\n",
        "application/pdf": b"%PDF-",
        "audio/ogg": b"OggS",
        "audio/amr": b"#!AMR",
    }
    if mime in signatures:
        return data.startswith(signatures[mime])
    if mime == "image/webp":
        return data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if mime in {"audio/mp4", "video/mp4", "video/3gpp"}:
        return data[4:8] == b"ftyp"
    if mime in {"audio/mpeg", "audio/aac"}:
        return data.startswith(b"ID3") or (
            len(data) > 1 and data[0] == 255 and data[1] & 224 == 224
        )
    if "openxmlformats" in mime:
        return data.startswith(b"PK\x03\x04")
    return data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
