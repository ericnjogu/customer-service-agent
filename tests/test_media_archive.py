import asyncio
import hashlib
import io
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from app.adapters.google_drive import API, SCOPE, ArchiveError, GoogleDrive
from app.adapters.postgres import schema
from app.config import Settings
from app.media_archive import MediaArchive, valid_signature
from app.whatsapp_coexistence import handle_coexistence, import_pending
from app.whatsapp_connections import PostgresWhatsAppConnections


class Vault:
    def __init__(self):
        self.tokens = {}

    async def write_drive_token(self, key, token):
        self.tokens[key] = token

    async def read_drive_token(self, key):
        return self.tokens[key]

    async def read(self, key):
        return SimpleNamespace(access_token=SecretStr("meta-test-token"))


class Google:
    def __init__(self):
        self.files = {}
        self.counter = 0
        self.uploads = 0

    def authorization_url(self, state):
        return state

    async def exchange(self, code):
        return SecretStr("access"), SecretStr("refresh")

    async def account(self, token):
        return "account-id"

    async def generate_id(self, token):
        self.counter += 1
        return f"file-{self.counter}"

    async def private(self, token, key):
        pass

    async def folder(self, token, key, name, parent=None):
        self.files[key] = {"id": key}

    async def metadata(self, token, key):
        if key not in self.files:
            raise ArchiveError("drive_file_unavailable")
        return self.files[key]

    async def refresh(self, token):
        return SecretStr("access")

    async def begin_upload(self, token, key, parent, name, mime, size):
        return key

    async def upload(self, token, key, file, size, mime):
        file.seek(0)
        content = file.read()
        self.uploads += 1
        self.files[key] = {"md5Checksum": hashlib.md5(content).hexdigest(), "id": key}


@pytest.fixture
async def archive():
    url = os.getenv("AGENT_WHATSAPP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set AGENT_WHATSAPP_TEST_DATABASE_URL to disposable pgvector PostgreSQL")
    namespace = "archive_" + uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    await admin.execute(f'CREATE SCHEMA "{namespace}"')
    pool = await asyncpg.create_pool(url, server_settings={"search_path": f"{namespace},public"})
    await pool.execute(schema(64))
    repo = PostgresWhatsAppConnections(pool)
    await repo.initialize()
    google, vault = Google(), Vault()
    png = b"\x89PNG\r\n\x1a\n" + b"test image"

    def meta(request):
        assert request.headers["authorization"] == "Bearer meta-test-token"
        if request.url.host == "graph.facebook.com":
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/media",
                    "mime_type": "image/png",
                    "file_size": len(png),
                },
            )
        return httpx.Response(200, content=png)

    client = httpx.AsyncClient(
        base_url="https://graph.facebook.com", transport=httpx.MockTransport(meta)
    )
    service = MediaArchive(pool, vault, google, Settings(), client)
    await service.initialize()
    try:
        yield service, repo
    finally:
        try:
            await service.close()
        finally:
            await client.aclose()
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{namespace}" CASCADE')
            await admin.close()


async def ready(service):
    session = await service.pool.fetchval(
        "INSERT INTO onboarding_sessions(admin_email) "
        "VALUES('test@example.com') RETURNING session_id"
    )
    await service.preferences(session, True)
    state = await service.authorize(session, "browser")
    await service.complete(session, "browser", state, SecretStr("code"))
    drive = await service.pool.fetchval(
        "SELECT id FROM drive_connections WHERE session_id=$1", session
    )
    connection = uuid4()
    await service.pool.execute(
        "INSERT INTO "
        "whatsapp_connections(connection_id,session_id,waba_id,phone_number_id,"
        "credential_reference,coexistence,import_history_enabled,archive_media_enabled,"
        "drive_connection_id) "
        "VALUES($1,$2,'123','456',$3,true,true,true,$4)",
        connection,
        session,
        f"whatsapp/{connection}",
        drive,
    )
    return session, connection


def history():
    return {
        "history": [
            {
                "threads": [
                    {
                        "id": "254700000001",
                        "messages": [
                            {
                                "id": "media-message",
                                "from": "254700000001",
                                "timestamp": "1700000000",
                                "type": "image",
                                "image": {"id": "999"},
                            }
                        ],
                    }
                ]
            }
        ]
    }


async def test_oauth_requires_original_browser_and_is_single_use(archive):
    service, _ = archive
    session = await service.pool.fetchval(
        "INSERT INTO onboarding_sessions(admin_email) VALUES('x@example.com') RETURNING session_id"
    )
    await service.preferences(session, True)
    state = await service.authorize(session, "browser")
    with pytest.raises(ArchiveError, match="drive_attempt_invalid"):
        await service.complete(session, "other-browser", state, SecretStr("code"))
    await service.complete(session, "browser", state, SecretStr("code"))
    with pytest.raises(ArchiveError, match="drive_attempt_invalid"):
        await service.complete(session, "browser", state, SecretStr("code"))
    status = await service.status(session)
    assert status["drive_status"] == "ready"
    assert status["folder_name"].startswith("Ristoh CSS")
    assert len(service.google.files) == 7
    assert "refresh" not in json.dumps(status)


async def test_history_queue_dedup_upload_and_later_association(archive):
    service, repo = archive
    session, connection = await ready(service)
    await handle_coexistence(repo, "123", "456", "history", history())
    await handle_coexistence(repo, "123", "456", "history", history())
    assert await service.pool.fetchval("SELECT count(*) FROM message_attachments") == 1
    assert await service.pool.fetchval("SELECT count(*) FROM pgqueuer") == 1
    await service.start()
    for _ in range(100):
        if await service.pool.fetchval("SELECT status FROM message_attachments") == "complete":
            break
        await asyncio.sleep(0.05)
    assert await service.pool.fetchval("SELECT status FROM message_attachments") == "complete"
    assert service.google.uploads == 1
    assert await service.pool.fetchval("SELECT count(*) FROM messages") == 0
    await service.pool.execute(
        "INSERT INTO tenants(tenant_id,slug,display_name) VALUES('tenant','tenant','Test')"
    )
    async with service.pool.acquire() as sql, sql.transaction():
        await import_pending(sql, connection, "tenant")
    linked = await service.pool.fetchrow("SELECT * FROM message_attachments")
    assert linked["tenant_id"] == "tenant"
    assert linked["message_id"] and linked["conversation_id"]
    assert (await service.status(session))["transfers"]["complete"] == 1


async def test_opt_out_ignores_history_and_locks_preferences(archive):
    service, repo = archive
    session, connection = await ready(service)
    await service.pool.execute(
        "UPDATE whatsapp_connections SET import_history_enabled=false WHERE connection_id=$1",
        connection,
    )
    await handle_coexistence(repo, "123", "456", "history", history())
    assert await service.pool.fetchval("SELECT count(*) FROM whatsapp_imported_messages") == 0
    await service.pool.execute(
        "UPDATE whatsapp_archive_preferences SET locked=true WHERE session_id=$1", session
    )
    with pytest.raises(ArchiveError, match="archive_preferences_locked"):
        await service.preferences(session, False)


async def test_media_unavailable_and_wrong_connection(archive):
    service, repo = archive
    await ready(service)
    payload = history()
    del payload["history"][0]["threads"][0]["messages"][0]["image"]["id"]
    await handle_coexistence(repo, "other", "456", "history", payload)
    assert await service.pool.fetchval("SELECT count(*) FROM message_attachments") == 0
    await handle_coexistence(repo, "123", "456", "history", payload)
    assert (
        await service.pool.fetchval("SELECT error_code FROM message_attachments")
        == "media_unavailable"
    )
    assert await service.pool.fetchval("SELECT count(*) FROM pgqueuer") == 0


async def test_crash_after_upload_does_not_duplicate_file(archive):
    service, repo = archive
    await ready(service)
    await handle_coexistence(repo, "123", "456", "history", history())
    key = await service.pool.fetchval("SELECT id FROM message_attachments")
    upload = service.google.upload

    async def crash(*args):
        await upload(*args)
        raise ArchiveError("connection_lost", retryable=True)

    service.google.upload = crash
    job = SimpleNamespace(payload=json.dumps({"id": str(key)}))
    with pytest.raises(ArchiveError):
        await service.process(job)
    service.google.upload = upload
    await asyncio.gather(service.process(job), service.process(job))
    assert service.google.uploads == 1
    assert await service.pool.fetchval("SELECT status FROM message_attachments") == "complete"


async def test_live_media_and_echo_keep_conversation_links(archive):
    service, repo = archive
    _, connection = await ready(service)
    await service.pool.execute(
        "INSERT INTO tenants(tenant_id,slug,display_name) VALUES('tenant','tenant','Test')"
    )
    await service.pool.execute(
        "UPDATE whatsapp_connections SET tenant_id='tenant' WHERE connection_id=$1", connection
    )
    message = history()["history"][0]["threads"][0]["messages"][0]
    await handle_coexistence(repo, "123", "456", "messages", {"messages": [message]})
    echo = {**message, "id": "echo-media", "from": "254700000009", "to": "254700000001"}
    await handle_coexistence(repo, "123", "456", "smb_message_echoes", {"message_echoes": [echo]})
    assert (
        await service.pool.fetchval(
            "SELECT count(DISTINCT conversation_id) FROM message_attachments"
        )
        == 1
    )
    assert (
        await service.pool.fetchval(
            "SELECT count(*) FROM message_attachments WHERE message_id IS NOT NULL"
        )
        == 2
    )
    assert await repo.is_paused("123", "456", "254700000001")
    assert (
        await service.pool.fetchval(
            "SELECT count(*) FROM pgqueuer WHERE entrypoint <> 'whatsapp_media_archive'"
        )
        == 0
    )


async def test_failed_transfer_explicit_replay_is_idempotent(archive):
    service, repo = archive
    session, _ = await ready(service)
    await handle_coexistence(repo, "123", "456", "history", history())
    await service.pool.execute("UPDATE message_attachments SET status='failed'")
    # A pending automatic retry is not duplicated by explicit replay.
    await service.replay_failed(session)
    assert await service.pool.fetchval("SELECT count(*) FROM pgqueuer") == 1
    await service.pool.execute("UPDATE pgqueuer SET status='failed'")
    await service.replay_failed(session)
    await service.replay_failed(session)
    assert await service.pool.fetchval("SELECT count(*) FROM pgqueuer") == 1
    assert await service.pool.fetchval("SELECT status FROM message_attachments") == "pending"


async def test_google_resumable_upload_uses_existing_offset():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(308, headers={"Range": "bytes=0-2"})
        return httpx.Response(200, json={"id": "file"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        google = GoogleDrive(client, "id", SecretStr("secret"), "https://example.com/callback")
        await google.upload(
            SecretStr("token"), API + "/upload/test", io.BytesIO(b"abcdef"), 6, "text/plain"
        )
    assert requests[1].content == b"def"
    assert requests[1].headers["content-range"] == "bytes 3-5/6"


@pytest.mark.parametrize(
    "status,error,expected,retryable",
    [
        (400, "invalid_grant", "drive_reconnect_required", False),
        (429, {}, "provider_unavailable", True),
        (503, {}, "provider_unavailable", True),
    ],
)
async def test_google_failure_classification(status, error, expected, retryable):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json={"error": error}))
    ) as client:
        google = GoogleDrive(client, "id", SecretStr("secret"), "https://example.com/callback")
        with pytest.raises(ArchiveError) as caught:
            await google.refresh(SecretStr("refresh"))
        assert caught.value.code == expected
        assert caught.value.retryable is retryable


async def test_download_rejects_untrusted_host_without_sending_token():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"url": "https://attacker.example/file"})

    async with httpx.AsyncClient(
        base_url="https://graph.facebook.com", transport=httpx.MockTransport(handler)
    ) as client:
        service = SimpleNamespace(meta_client=client, settings=Settings())
        with pytest.raises(ArchiveError, match="invalid_media_url"):
            await MediaArchive.download(service, SecretStr("token"), "123", None)
    assert len(requests) == 1


@pytest.mark.parametrize("declared_size", [1, 1000])
async def test_download_enforces_declared_and_streamed_size(declared_size):
    def handler(request):
        if request.url.host == "graph.facebook.com":
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/media",
                    "mime_type": "image/png",
                    "file_size": declared_size,
                },
            )
        return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"x" * 100)

    async with httpx.AsyncClient(
        base_url="https://graph.facebook.com", transport=httpx.MockTransport(handler)
    ) as client:
        service = SimpleNamespace(meta_client=client, settings=Settings(media_archive_max_bytes=10))
        with pytest.raises(ArchiveError, match="media_too_large"):
            await MediaArchive.download(service, SecretStr("token"), "123", io.BytesIO())


async def test_google_token_exchange_refresh_and_limited_scope():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"access_token": "access", "refresh_token": "refresh", "scope": SCOPE}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        google = GoogleDrive(client, "client", SecretStr("secret"), "https://example.com/callback")
        access, refresh = await google.exchange(SecretStr("code"))
        assert await google.refresh(refresh) == access
        url = google.authorization_url("state")
        assert "drive.file" in url and "access_type=offline" in url
        assert len(requests) == 2


async def test_google_rejects_shared_folder_and_classifies_quota():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"ownedByMe": True, "permissions": [{"role": "reader"}]}
            )
        return httpx.Response(403, json={"error": {"errors": [{"reason": "storageQuotaExceeded"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        google = GoogleDrive(client, "client", SecretStr("secret"), "https://example.com/callback")
        with pytest.raises(ArchiveError, match="drive_folder_not_private"):
            await google.private(SecretStr("token"), "folder")
        with pytest.raises(ArchiveError, match="drive_quota_exceeded"):
            await google.request("POST", API + "/test")


@pytest.mark.parametrize(
    "mime,content,valid",
    [
        ("image/png", b"\x89PNG\r\n\x1a\n", True),
        ("image/png", b"MZ executable", False),
        ("application/pdf", b"%PDF-1.7", True),
        ("text/plain", b"\x7fELF", False),
    ],
)
def test_content_type_checks(mime, content, valid):
    assert valid_signature(mime, content) is valid
