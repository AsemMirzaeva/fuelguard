from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.workers import names

setup_logging()
settings = get_settings()

celery_app = Celery(
    "fuelguard",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "recover-stuck-transactions": {
            "task": names.RECOVER_STUCK,
            "schedule": settings.recovery_interval_seconds,
        },
        "daily-report": {
            "task": names.DAILY_REPORT,
            "schedule": crontab(hour=0, minute=10),  # 00:10 UTC, for the day before
        },
    },
)
