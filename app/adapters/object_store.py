import logging
from dataclasses import dataclass
from functools import partial
from typing import Any

import anyio

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    etag: str | None = None


class MemoryKnowledgeObjectStore:
    def __init__(self, bucket: str = "knowledge") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    async def initialize(self) -> None:
        return None

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> StoredObject:
        self.objects[key] = content
        self.metadata[key] = {"content_type": content_type, **metadata}
        logger.info(
            "Stored knowledge object in memory bucket=%s key=%s content_type=%s bytes=%d",
            self.bucket,
            key,
            content_type,
            len(content),
        )
        return StoredObject(bucket=self.bucket, key=key, etag=f"memory-{len(content)}")

    async def get(self, *, key: str) -> bytes:
        logger.info("Loading knowledge object from memory bucket=%s key=%s", self.bucket, key)
        return self.objects[key]


class S3KnowledgeObjectStore:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region_name: str = "us-east-1",
        secure: bool = False,
    ) -> None:
        self.bucket = bucket
        self.client = create_s3_client(
            endpoint_url=endpoint_url,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            region_name=region_name,
            secure=secure,
        )

    async def initialize(self) -> None:
        logger.info("Initializing S3 knowledge object store bucket=%s", self.bucket)
        await anyio.to_thread.run_sync(self._ensure_bucket)

    async def put(
        self,
        *,
        key: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, str],
    ) -> StoredObject:
        logger.info(
            "Storing knowledge object in S3 bucket=%s key=%s content_type=%s bytes=%d",
            self.bucket,
            key,
            content_type,
            len(content),
        )
        response = await anyio.to_thread.run_sync(
            partial(
                self.client.put_object,
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                Metadata=metadata,
            )
        )
        stored = StoredObject(
            bucket=self.bucket,
            key=key,
            etag=str(response.get("ETag", "")).strip('"') or None,
        )
        logger.info(
            "Stored knowledge object in S3 bucket=%s key=%s etag=%s",
            stored.bucket,
            stored.key,
            stored.etag or "<none>",
        )
        return stored

    async def get(self, *, key: str) -> bytes:
        logger.info("Loading knowledge object from S3 bucket=%s key=%s", self.bucket, key)
        response = await anyio.to_thread.run_sync(
            partial(
                self.client.get_object,
                Bucket=self.bucket,
                Key=key,
            )
        )
        body = response["Body"]
        try:
            content = await anyio.to_thread.run_sync(body.read)
            logger.info(
                "Loaded knowledge object from S3 bucket=%s key=%s bytes=%d",
                self.bucket,
                key,
                len(content),
            )
            return content
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

    def _ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            logger.info("S3 knowledge bucket already exists bucket=%s", self.bucket)
        except Exception:
            logger.info("Creating S3 knowledge bucket bucket=%s", self.bucket)
            self.client.create_bucket(Bucket=self.bucket)


def create_s3_client(
    *,
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
    region_name: str,
    secure: bool,
) -> Any:
    import boto3

    normalized_endpoint = endpoint_url
    if not normalized_endpoint.startswith(("http://", "https://")):
        scheme = "https" if secure else "http"
        normalized_endpoint = f"{scheme}://{normalized_endpoint}"

    return boto3.client(
        "s3",
        endpoint_url=normalized_endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region_name,
    )
