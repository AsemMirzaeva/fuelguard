"""Synchronous database logic used by the Celery tasks.

Every function here is safe to run twice for the same transaction: the row is
locked, state is checked first, and flags have a unique constraint.
"""

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import exists, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    DailyReport,
    Driver,
    FuelCard,
    FuelTransaction,
    Notification,
    TransactionFlag,
    Truck,
)
from app.models.enums import FlagReason, TransactionStatus
from app.services import risk_rules
from app.services.risk_rules import FlagResult
from app.services.stuck import stuck_condition

logger = logging.getLogger(__name__)

# Statuses that count as real purchases for spending totals and averages.
ACCEPTED = (TransactionStatus.PENDING, TransactionStatus.APPROVED)
RECOVERY_BATCH = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_transaction(session: Session, tx_id: int) -> FuelTransaction | None:
    stmt = select(FuelTransaction).where(FuelTransaction.id == tx_id).with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def _position(tx: FuelTransaction):
    # (time, id) gives a stable order even when two purchases share a timestamp.
    return tuple_(tx.transaction_time, tx.id)


def _row():
    return tuple_(FuelTransaction.transaction_time, FuelTransaction.id)


def _spent(session: Session, tx: FuelTransaction) -> tuple[Decimal, Decimal, Decimal]:
    t = tx.transaction_time
    total = func.sum(FuelTransaction.total_price)
    stmt = select(
        func.coalesce(total.filter(FuelTransaction.transaction_time > t - timedelta(days=1)), 0),
        func.coalesce(total.filter(FuelTransaction.transaction_time > t - timedelta(days=7)), 0),
        func.coalesce(total, 0),
    ).where(
        FuelTransaction.card_id == tx.card_id,
        FuelTransaction.status.in_(ACCEPTED),
        FuelTransaction.transaction_time > t - timedelta(days=30),
        _row() <= _position(tx),
    )
    day, week, month = session.execute(stmt).one()
    return Decimal(day), Decimal(week), Decimal(month)


def _driver_history(session: Session, tx: FuelTransaction) -> tuple[int, Decimal | None]:
    stmt = select(func.count(), func.avg(FuelTransaction.gallons)).where(
        FuelTransaction.driver_id == tx.driver_id,
        FuelTransaction.status.in_(ACCEPTED),
        _row() < _position(tx),
    )
    count, avg = session.execute(stmt).one()
    return count, Decimal(avg) if avg is not None else None


def _attempts(session: Session, tx: FuelTransaction, column, value: int, minutes: int) -> int:
    """All attempts (declined too) by one card or truck in the window ending at tx."""
    stmt = select(func.count()).where(
        column == value,
        FuelTransaction.transaction_time > tx.transaction_time - timedelta(minutes=minutes),
        _row() <= _position(tx),
    )
    return session.execute(stmt).scalar_one()


def _frequency_flags(session: Session, tx: FuelTransaction) -> list[FlagResult]:
    s = get_settings()
    results = [
        risk_rules.check_frequency(
            _attempts(session, tx, FuelTransaction.card_id, tx.card_id, s.frequency_window_minutes),
            s.frequency_max_purchases,
            s.frequency_window_minutes,
            FlagReason.CARD_FREQUENCY,
        )
    ]
    if tx.truck_id is not None:
        results.append(
            risk_rules.check_frequency(
                _attempts(
                    session, tx, FuelTransaction.truck_id, tx.truck_id, s.frequency_window_minutes
                ),
                s.frequency_max_purchases,
                s.frequency_window_minutes,
                FlagReason.TRUCK_FREQUENCY,
            )
        )
    return [r for r in results if r is not None]


def _insert_flags(session: Session, tx_id: int, results: list[FlagResult]) -> int:
    """Returns how many flags were newly created (existing ones are left alone)."""
    if not results:
        return 0
    stmt = (
        pg_insert(TransactionFlag)
        .values(
            [{"transaction_id": tx_id, "reason": r.reason, "details": r.details} for r in results]
        )
        .on_conflict_do_nothing(constraint="uq_transaction_flags_transaction_reason")
        .returning(TransactionFlag.id)
    )
    return len(session.execute(stmt).all())


def analyze_transaction(session: Session, tx_id: int) -> bool:
    """Run all risk rules on a pending purchase and mark it approved.

    Returns True when the purchase got at least one flag.
    """
    tx = _lock_transaction(session, tx_id)
    if tx is None:
        logger.warning("analyze: transaction %s not found", tx_id)
        return False
    if tx.status != TransactionStatus.PENDING:
        return False  # already analyzed (retry or duplicate message)

    s = get_settings()
    card = session.get(FuelCard, tx.card_id)
    truck = session.get(Truck, tx.truck_id) if tx.truck_id is not None else None
    capacity = truck.tank_capacity_gal if truck else None

    day, week, month = _spent(session, tx)
    results: list[FlagResult] = risk_rules.check_limits(
        tx.total_price,
        day,
        week,
        month,
        card.per_transaction_limit,
        card.daily_limit,
        card.weekly_limit,
        card.monthly_limit,
    )
    if capacity is not None:
        results.append(risk_rules.check_tank(tx.gallons, capacity))
    history_count, history_avg = _driver_history(session, tx)
    results.append(
        risk_rules.check_large(
            tx.gallons,
            history_count,
            history_avg,
            capacity,
            s.large_purchase_multiplier,
            s.large_purchase_min_history,
            s.large_purchase_fallback_tank_ratio,
        )
    )
    results.extend(_frequency_flags(session, tx))

    created = _insert_flags(session, tx.id, [r for r in results if r is not None])
    tx.status = TransactionStatus.APPROVED
    tx.analysis_done_at = _now()
    logger.info("analyzed transaction %s: %s new flag(s)", tx.id, created)
    return created > 0


def check_declined_attempt(session: Session, tx_id: int) -> bool:
    """Burst check for a declined attempt, so a thief who never gets approved is still seen."""
    tx = _lock_transaction(session, tx_id)
    if tx is None:
        logger.warning("declined check: transaction %s not found", tx_id)
        return False
    if tx.status != TransactionStatus.DECLINED or tx.analysis_done_at is not None:
        return False

    created = _insert_flags(session, tx.id, _frequency_flags(session, tx))
    tx.analysis_done_at = _now()
    logger.info("checked declined attempt %s: %s new flag(s)", tx.id, created)
    return created > 0


def create_notification(session: Session, tx_id: int) -> bool:
    """One notification per flagged transaction. Returns True if one was created."""
    tx = _lock_transaction(session, tx_id)
    if tx is None:
        return False
    already = session.execute(
        select(exists().where(Notification.transaction_id == tx_id))
    ).scalar_one()
    if already:
        return False

    reasons = list(
        session.execute(
            select(TransactionFlag.reason)
            .where(TransactionFlag.transaction_id == tx_id)
            .order_by(TransactionFlag.id)
        ).scalars()
    )
    if not reasons:
        return False

    card = session.get(FuelCard, tx.card_id)
    driver = session.get(Driver, tx.driver_id)
    message = (
        f"Suspicious transaction #{tx.id} ({tx.status.value}): "
        f"{', '.join(r.value for r in reasons)}. "
        f"Card {card.card_number}, driver {driver.full_name}, "
        f"{tx.gallons} gal, {tx.total_price}."
    )
    session.add(Notification(transaction_id=tx.id, message=message))
    logger.warning("ALERT %s", message)
    return True


def build_daily_report(session: Session, report_date: date) -> int:
    """One row per card with activity on report_date (UTC). Safe to re-run: rows are updated."""
    start = datetime.combine(report_date, time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    flagged = exists().where(TransactionFlag.transaction_id == FuelTransaction.id)

    stmt = (
        select(
            FuelTransaction.card_id,
            func.sum(FuelTransaction.gallons),
            func.sum(FuelTransaction.total_price),
            func.count(),
            func.count().filter(flagged),
        )
        .where(
            FuelTransaction.status.in_(ACCEPTED),
            FuelTransaction.transaction_time >= start,
            FuelTransaction.transaction_time < end,
        )
        .group_by(FuelTransaction.card_id)
    )
    rows = [
        {
            "report_date": report_date,
            "card_id": card_id,
            "total_gallons": gallons,
            "total_spend": spend,
            "transaction_count": count,
            "flagged_count": flagged_count,
        }
        for card_id, gallons, spend, count, flagged_count in session.execute(stmt).all()
    ]
    if rows:
        insert = pg_insert(DailyReport).values(rows)
        session.execute(
            insert.on_conflict_do_update(
                constraint="uq_daily_reports_date_card",
                set_={
                    "total_gallons": insert.excluded.total_gallons,
                    "total_spend": insert.excluded.total_spend,
                    "transaction_count": insert.excluded.transaction_count,
                    "flagged_count": insert.excluded.flagged_count,
                },
            )
        )
    return len(rows)


def find_stuck(session: Session) -> tuple[list[tuple[int, TransactionStatus]], list[int]]:
    """Unfinished analysis, and flagged transactions that never got a notification."""
    cutoff = _now() - timedelta(minutes=get_settings().stuck_after_minutes)

    unfinished = session.execute(
        select(FuelTransaction.id, FuelTransaction.status)
        .where(stuck_condition(cutoff))
        .order_by(FuelTransaction.id)
        .limit(RECOVERY_BATCH)
    ).all()

    unnotified = list(
        session.execute(
            select(TransactionFlag.transaction_id)
            .where(
                TransactionFlag.created_at < cutoff,
                ~exists().where(Notification.transaction_id == TransactionFlag.transaction_id),
            )
            .distinct()
            .limit(RECOVERY_BATCH)
        ).scalars()
    )
    return [(i, st) for i, st in unfinished], unnotified
