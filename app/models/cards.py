from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CardStatus, db_enum


class FuelCard(Base):
    __tablename__ = "fuel_cards"
    __table_args__ = (
        CheckConstraint(
            "(per_transaction_limit IS NULL OR per_transaction_limit > 0)"
            " AND (daily_limit IS NULL OR daily_limit > 0)"
            " AND (weekly_limit IS NULL OR weekly_limit > 0)"
            " AND (monthly_limit IS NULL OR monthly_limit > 0)",
            name="limits_positive",
        ),
        Index("ix_fuel_cards_driver_id", "driver_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    card_number: Mapped[str] = mapped_column(String(32), unique=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"))
    status: Mapped[CardStatus] = mapped_column(
        db_enum(CardStatus, 20), default=CardStatus.ACTIVE
    )
    per_transaction_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    daily_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    weekly_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    monthly_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CardStatusHistory(Base):
    __tablename__ = "card_status_history"
    __table_args__ = (Index("ix_card_status_history_card_id", "card_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("fuel_cards.id"))
    old_status: Mapped[CardStatus | None] = mapped_column(db_enum(CardStatus, 20))
    new_status: Mapped[CardStatus] = mapped_column(db_enum(CardStatus, 20))
    changed_by: Mapped[str] = mapped_column(String(50), default="manager")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
