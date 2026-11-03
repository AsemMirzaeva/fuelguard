from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import InvalidStateError, NotFoundError
from app.models import FuelTransaction, TransactionFlag
from app.models.enums import FlagReason, ReviewStatus
from app.services.common import get_or_404
from app.services.stuck import stuck_condition


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


async def load_detail(db: AsyncSession, transaction_id: int) -> FuelTransaction:
    stmt = (
        select(FuelTransaction)
        .where(FuelTransaction.id == transaction_id)
        .options(selectinload(FuelTransaction.flags))
        .execution_options(populate_existing=True)
    )
    tx = (await db.execute(stmt)).scalar_one_or_none()
    if tx is None:
        raise NotFoundError(f"Transaction {transaction_id} not found")
    return tx


async def list_flagged(
    db: AsyncSession,
    reason: FlagReason | None,
    review_status: ReviewStatus | None,
    card_id: int | None,
    driver_id: int | None,
    truck_id: int | None,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    offset: int,
) -> list[FuelTransaction]:
    has_flag = exists().where(TransactionFlag.transaction_id == FuelTransaction.id)
    if reason is not None:
        has_flag = has_flag.where(TransactionFlag.reason == reason)

    stmt = (
        select(FuelTransaction)
        .where(has_flag)
        .options(selectinload(FuelTransaction.flags))
        .order_by(FuelTransaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    for column, value in (
        (FuelTransaction.review_status, review_status),
        (FuelTransaction.card_id, card_id),
        (FuelTransaction.driver_id, driver_id),
        (FuelTransaction.truck_id, truck_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if date_from is not None:
        stmt = stmt.where(FuelTransaction.transaction_time >= _day_start(date_from))
    if date_to is not None:
        stmt = stmt.where(FuelTransaction.transaction_time < _day_start(date_to) + timedelta(days=1))
    return list((await db.execute(stmt)).scalars())


async def review(
    db: AsyncSession, transaction_id: int, review_status: ReviewStatus, note: str | None
) -> FuelTransaction:
    tx = await get_or_404(db, FuelTransaction, transaction_id, "Transaction", lock=True)
    flagged = await db.scalar(
        select(exists().where(TransactionFlag.transaction_id == transaction_id))
    )
    if not flagged:
        raise InvalidStateError("Only flagged transactions can be reviewed")

    tx.review_status = review_status
    tx.review_note = note
    tx.reviewed_at = datetime.now(timezone.utc)
    await db.commit()
    return await load_detail(db, transaction_id)


async def list_stuck(
    db: AsyncSession, older_than_minutes: int | None, limit: int, offset: int
) -> list[FuelTransaction]:
    minutes = older_than_minutes if older_than_minutes is not None else get_settings().stuck_after_minutes
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    stmt = (
        select(FuelTransaction)
        .where(stuck_condition(cutoff))
        .order_by(FuelTransaction.id)
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(stmt)).scalars())
