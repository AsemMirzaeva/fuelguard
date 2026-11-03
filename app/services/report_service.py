from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DailyReport, Notification
from app.services.common import get_or_404


async def list_notifications(
    db: AsyncSession, unread: bool | None, limit: int, offset: int
) -> list[Notification]:
    stmt = select(Notification).order_by(Notification.id.desc()).limit(limit).offset(offset)
    if unread is True:
        stmt = stmt.where(Notification.read_at.is_(None))
    elif unread is False:
        stmt = stmt.where(Notification.read_at.is_not(None))
    return list((await db.execute(stmt)).scalars())


async def mark_read(db: AsyncSession, notification_id: int) -> Notification:
    notification = await get_or_404(db, Notification, notification_id, "Notification", lock=True)
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        await db.commit()
    return notification


async def list_daily_reports(
    db: AsyncSession, report_date: date | None, card_id: int | None, limit: int, offset: int
) -> list[DailyReport]:
    stmt = (
        select(DailyReport)
        .order_by(DailyReport.report_date.desc(), DailyReport.card_id)
        .limit(limit)
        .offset(offset)
    )
    if report_date is not None:
        stmt = stmt.where(DailyReport.report_date == report_date)
    if card_id is not None:
        stmt = stmt.where(DailyReport.card_id == card_id)
    return list((await db.execute(stmt)).scalars())
