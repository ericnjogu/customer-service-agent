from app.adapters.ingestion_queue import (
    MemoryKnowledgeIngestionQueue,
    RedisKnowledgeIngestionQueue,
)


class FakeRedisQueueClient:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.closed = False

    async def lpush(self, queue_name: str, job_id: str) -> None:
        self.values.insert(0, f"{queue_name}:{job_id}")

    async def brpop(self, queue_name: str, timeout: int):
        if not self.values:
            return None
        value = self.values.pop()
        _, job_id = value.split(":", maxsplit=1)
        return queue_name, job_id

    async def aclose(self) -> None:
        self.closed = True


async def test_memory_knowledge_ingestion_queue_round_trips_job_ids() -> None:
    queue = MemoryKnowledgeIngestionQueue()

    await queue.enqueue("job-1")

    assert await queue.dequeue(timeout_seconds=1) == "job-1"


async def test_redis_knowledge_ingestion_queue_uses_configured_queue_name() -> None:
    redis = FakeRedisQueueClient()
    queue = RedisKnowledgeIngestionQueue(redis, queue_name="kb-jobs")

    await queue.enqueue("job-1")
    job_id = await queue.dequeue(timeout_seconds=1)
    await queue.close()

    assert job_id == "job-1"
    assert redis.closed is True

