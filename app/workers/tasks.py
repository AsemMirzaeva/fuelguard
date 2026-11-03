import logging
from datetime import date, datetime, timedelta, timezone

from celery import Task
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.config import get_settings
from app.db.sync_db import session_scope
from app.models.enums import TransactionStatus
from app.workers import names, risk_analysis
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()


class LoggedFailureTask(Task):
    """Logs when retries run out. The data is safe: the recovery job tries again later."""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error(
            "Task %s%s gave up: %r. The transaction stays unanalyzed; the recovery job will re-queue it.",
            self.name,
            args,
            exc,
        )


# Retry only on temporary database trouble, with exponential backoff and jitter.
RETRY = dict(
    base=LoggedFailureTask,
    autoretry_for=(OperationalError, InterfaceError),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=settings.analysis_max_retries,
)


def _queue_notification(tx_id: int) -> None:
    try:
        notify_suspicious_transaction.delay(tx_id)
    except Exception:
        # The flags are already saved; the recovery job finds flagged
        # transactions without a notification.
        logger.exception("Could not queue notification for transaction %s", tx_id)


@celery_app.task(name="fuelguard.ping")
def ping() -> str:
    """Smoke-test task: confirms the worker is connected to the broker."""
    return "pong"


@celery_app.task(name=names.ANALYZE_TRANSACTION, bind=True, **RETRY)
def analyze_transaction(self, tx_id: int) -> None:
    with session_scope() as session:
        flagged = risk_analysis.analyze_transaction(session, tx_id)
    if flagged:
        _queue_notification(tx_id)


@celery_app.task(name=names.CHECK_DECLINED_ATTEMPT, bind=True, **RETRY)
def check_declined_attempt(self, tx_id: int) -> None:
    with session_scope() as session:
        flagged = risk_analysis.check_declined_attempt(session, tx_id)
    if flagged:
        _queue_notification(tx_id)


@celery_app.task(name=names.NOTIFY_SUSPICIOUS, bind=True, **RETRY)
def notify_suspicious_transaction(self, tx_id: int) -> None:
    with session_scope() as session:
        risk_analysis.create_notification(session, tx_id)


@celery_app.task(name=names.DAILY_REPORT, bind=True, **RETRY)
def daily_report(self, report_date: str | None = None) -> int:
    """Totals per card for one UTC day. Defaults to yesterday."""
    day = (
        date.fromisoformat(report_date)
        if report_date
        else datetime.now(timezone.utc).date() - timedelta(days=1)
    )
    with session_scope() as session:
        rows = risk_analysis.build_daily_report(session, day)
    logger.info("daily report for %s: %s card(s)", day, rows)
    return rows


@celery_app.task(name=names.RECOVER_STUCK, bind=True, **RETRY)
def recover_stuck_transactions(self) -> dict:
    """Re-queue work that never finished (worker or broker was down).

    Safe to run often: the tasks it queues do nothing for work already done.
    """
    with session_scope() as session:
        unfinished, unnotified = risk_analysis.find_stuck(session)

    for tx_id, status in unfinished:
        task = analyze_transaction if status == TransactionStatus.PENDING else check_declined_attempt
        task.delay(tx_id)
    for tx_id in unnotified:
        notify_suspicious_transaction.delay(tx_id)

    if unfinished or unnotified:
        logger.warning(
            "recovery: re-queued %s unfinished and %s unnotified transaction(s)",
            len(unfinished),
            len(unnotified),
        )
    return {"unfinished": len(unfinished), "unnotified": len(unnotified)}
