import asyncio
import logging

from app.config import get_settings
from app.container import create_container
from app.main import configure_logging

logger = logging.getLogger(__name__)


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    container = await create_container(settings)
    logger.info(
        "Starting knowledge ingestion worker queue_provider=%s queue_name=%s",
        settings.knowledge_ingestion_queue_provider,
        settings.knowledge_ingestion_queue_name,
    )
    try:
        while True:
            job_id = await container.knowledge_ingestion_queue.dequeue(
                timeout_seconds=settings.knowledge_ingestion_worker_poll_seconds
            )
            if not job_id:
                logger.trace(
                    "No knowledge ingestion job available queue_name=%s poll_seconds=%d",
                    settings.knowledge_ingestion_queue_name,
                    settings.knowledge_ingestion_worker_poll_seconds,
                )
                continue
            logger.info("Dequeued knowledge ingestion job_id=%s", job_id)
            await container.knowledge_ingestion_worker.run_job(job_id)
    finally:
        await container.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
