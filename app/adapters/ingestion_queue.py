import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class MemoryKnowledgeIngestionQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)
        logger.info("Enqueued knowledge ingestion job in memory queue job_id=%s", job_id)

    async def dequeue(self, *, timeout_seconds: int = 5) -> str | None:
        try:
            job_id = await asyncio.wait_for(self.queue.get(), timeout=timeout_seconds)
            logger.info("Dequeued knowledge ingestion job from memory queue job_id=%s", job_id)
            return job_id
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        return None


class RedisKnowledgeIngestionQueue:
    def __init__(
        self,
        redis_client: Any,
        *,
        queue_name: str = "knowledge-ingestion-jobs",
    ) -> None:
        self.redis = redis_client
        self.queue_name = queue_name

    async def enqueue(self, job_id: str) -> None:
        await self.redis.lpush(self.queue_name, job_id)
        logger.info(
            "Enqueued knowledge ingestion job in Redis queue=%s job_id=%s",
            self.queue_name,
            job_id,
        )

    async def dequeue(self, *, timeout_seconds: int = 5) -> str | None:
        item = await self.redis.brpop(self.queue_name, timeout=timeout_seconds)
        if item is None:
            return None
        _, job_id = item
        if isinstance(job_id, bytes):
            decoded_job_id = job_id.decode("utf-8")
        else:
            decoded_job_id = str(job_id)
        logger.info(
            "Dequeued knowledge ingestion job from Redis queue=%s job_id=%s",
            self.queue_name,
            decoded_job_id,
        )
        return decoded_job_id

    async def close(self) -> None:
        close = getattr(self.redis, "aclose", None)
        if close:
            await close()
