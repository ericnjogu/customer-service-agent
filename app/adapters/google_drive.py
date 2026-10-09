"""Small Drive REST adapter using the application's existing async HTTP transport.

All credential-bearing requests suppress tracing through secret_transport. Provider
errors are sanitized; neither refresh tokens nor resumable URLs belong in logs.
"""

from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import SecretStr

from app.adapters.secret_transport import secret_transport

SCOPE = "https://www.googleapis.com/auth/drive.file"
API = "https://www.googleapis.com"
TOKEN = "https://oauth2.googleapis.com/token"


class ArchiveError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class GoogleDrive:
    def __init__(self, client, client_id, client_secret, redirect_uri, test_base_url=None):
        self.client = client
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.test_base_url = test_base_url

    def authorization_url(self, state):
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )

    async def request(self, method, url, *, token=None, **kwargs):
        if self.test_base_url:
            if url.startswith(API):
                url = self.test_base_url + url[len(API) :]
            elif url == TOKEN:
                url = self.test_base_url + "/google/token"
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        try:
            with secret_transport():
                response = await self.client.request(method, url, headers=headers, **kwargs)
            if response.status_code >= 400:
                if response.status_code == 409:
                    raise ArchiveError("drive_conflict", retryable=True)
                if response.status_code == 429 or response.status_code >= 500:
                    raise ArchiveError("provider_unavailable", retryable=True)
                if response.status_code == 401:
                    raise ArchiveError("drive_reconnect_required")
                if response.status_code == 404:
                    raise ArchiveError("drive_file_unavailable")
                # Read only known machine codes; never propagate provider body text.
                try:
                    error = response.json().get("error", {})
                    if error == "invalid_grant":
                        raise ArchiveError("drive_reconnect_required")
                    reasons = (
                        [e.get("reason") for e in error.get("errors", [])]
                        if isinstance(error, dict)
                        else []
                    )
                    if "storageQuotaExceeded" in reasons:
                        raise ArchiveError("drive_quota_exceeded")
                    if any(r in reasons for r in ["rateLimitExceeded", "userRateLimitExceeded"]):
                        raise ArchiveError("provider_unavailable", retryable=True)
                except ValueError:
                    pass
                raise ArchiveError("drive_access_denied")
            return response
        except httpx.HTTPError:
            raise ArchiveError("provider_unavailable", retryable=True) from None

    async def exchange(self, code):
        response = await self.request(
            "POST",
            TOKEN,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret.get_secret_value(),
                "redirect_uri": self.redirect_uri,
                "code": code.get_secret_value(),
                "grant_type": "authorization_code",
            },
        )
        data = response.json()
        if SCOPE not in data.get("scope", "").split() or not data.get("refresh_token"):
            raise ArchiveError("drive_consent_required")
        return SecretStr(data["access_token"]), SecretStr(data["refresh_token"])

    async def refresh(self, refresh_token):
        response = await self.request(
            "POST",
            TOKEN,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret.get_secret_value(),
                "refresh_token": refresh_token.get_secret_value(),
                "grant_type": "refresh_token",
            },
        )
        return SecretStr(response.json()["access_token"])

    async def account(self, token):
        response = await self.request(
            "GET",
            API + "/drive/v3/about",
            token=token,
            params={"fields": "user(permissionId,emailAddress)"},
        )
        return response.json()["user"]["permissionId"]

    async def generate_id(self, token):
        response = await self.request(
            "GET",
            API + "/drive/v3/files/generateIds",
            token=token,
            params={"count": 1, "space": "drive", "type": "files"},
        )
        return response.json()["ids"][0]

    async def metadata(self, token, file_id):
        response = await self.request(
            "GET",
            API + f"/drive/v3/files/{file_id}",
            token=token,
            params={"fields": "id,trashed,md5Checksum,size,permissions(type,role),ownedByMe"},
        )
        return response.json()

    async def private(self, token, file_id):
        data = await self.metadata(token, file_id)
        if (
            data.get("trashed")
            or not data.get("ownedByMe")
            or any(p.get("role") != "owner" for p in data.get("permissions", []))
        ):
            raise ArchiveError("drive_folder_not_private")

    async def folder(self, token, file_id, name, parent=None):
        body = {"id": file_id, "name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            body["parents"] = [parent]
        # Caller persists the generated ID before creation. A retry never creates a new folder.
        try:
            await self.metadata(token, file_id)
        except ArchiveError as error:
            if error.code != "drive_file_unavailable":
                raise
            await self.request(
                "POST",
                API + "/drive/v3/files",
                token=token,
                json=body,
                params={"fields": "id", "ignoreDefaultVisibility": "true"},
            )
        await self.private(token, file_id)

    async def begin_upload(self, token, file_id, parent, name, mime, size):
        response = await self.request(
            "POST",
            API + "/upload/drive/v3/files",
            token=token,
            params={"uploadType": "resumable", "fields": "id", "ignoreDefaultVisibility": "true"},
            headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
            json={"id": file_id, "name": name, "parents": [parent]},
        )
        url = response.headers.get("location", "")
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname != "www.googleapis.com"
            or parts.port not in (None, 443)
        ):
            raise ArchiveError("invalid_upload_url")
        return url

    async def upload(self, token, url, file, size, mime):
        # Query server offset before resuming. URLs are only provider-generated and
        # validated on both creation and use; they never originate in webhook payloads.
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname != "www.googleapis.com"
            or parts.port not in (None, 443)
        ):
            raise ArchiveError("invalid_upload_url")
        response = await self.request(
            "PUT",
            url,
            token=token,
            headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
            content=b"",
        )
        if response.status_code in (200, 201):
            return
        offset = (
            int(response.headers.get("range", "bytes=0--1").rsplit("-", 1)[-1]) + 1
            if "range" in response.headers
            else 0
        )
        file.seek(offset)
        while offset < size:
            chunk = file.read(1024 * 1024)
            response = await self.request(
                "PUT",
                url,
                token=token,
                headers={
                    "Content-Type": mime,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
                },
                content=chunk,
            )
            offset += len(chunk)
            if response.status_code not in (200, 201, 308):
                raise ArchiveError("upload_incomplete", retryable=True)
        if response.status_code not in (200, 201):
            raise ArchiveError("upload_incomplete", retryable=True)
