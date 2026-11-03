import asyncio
import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def enqueue(task_name: str, *args) -> bool:
    """Send a task to the queue without blocking the event loop.

    The transaction is already committed when this runs, so a broker outage must
    not fail the request. It is logged, and the recovery job picks up whatever
    stayed pending.
    """
    try:
        await asyncio.to_thread(celery_app.send_task, task_name, args=args, retry=False)
        return True
    except Exception:
        logger.exception("Could not queue %s%s; recovery job will retry", task_name, args)
        return False
