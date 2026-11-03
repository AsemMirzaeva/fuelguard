from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_transaction_id", "transaction_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("fuel_transactions.id"))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DailyReport(Base):
    __tablename__ = "daily_reports"
    __table_args__ = (
        # One row per card per day makes the nightly job safe to re-run.
        UniqueConstraint("report_date", "card_id", name="uq_daily_reports_date_card"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[date] = mapped_column(Date)
    card_id: Mapped[int] = mapped_column(ForeignKey("fuel_cards.id"))
    total_gallons: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    total_spend: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    transaction_count: Mapped[int]
    flagged_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
