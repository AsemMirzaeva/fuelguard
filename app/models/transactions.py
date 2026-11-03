from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    DeclineReason,
    FlagReason,
    ReviewStatus,
    TransactionStatus,
    db_enum,
)


class FuelStation(Base):
    __tablename__ = "fuel_stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(255))


class FuelTransaction(Base):
    __tablename__ = "fuel_transactions"
    __table_args__ = (
        CheckConstraint("gallons > 0", name="gallons_positive"),
        CheckConstraint("total_price >= 0", name="price_not_negative"),
        Index("ix_fuel_transactions_card_time", "card_id", "transaction_time"),
        Index("ix_fuel_transactions_truck_time", "truck_id", "transaction_time"),
        Index("ix_fuel_transactions_driver_id", "driver_id"),
        # Finds transactions the worker has not analyzed yet (recovery job).
        Index(
            "ix_fuel_transactions_pending",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_fuel_transactions_review_status", "review_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)

    # No relationship() loading by default: the driver and truck are a
    # snapshot taken at purchase time.
    card_id: Mapped[int] = mapped_column(ForeignKey("fuel_cards.id"))
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"))
    truck_id: Mapped[int | None] = mapped_column(ForeignKey("trucks.id"))
    station_id: Mapped[int] = mapped_column(ForeignKey("fuel_stations.id"))

    gallons: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    transaction_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    status: Mapped[TransactionStatus] = mapped_column(
        db_enum(TransactionStatus, 20), default=TransactionStatus.PENDING
    )
    decline_reason: Mapped[DeclineReason | None] = mapped_column(db_enum(DeclineReason))
    analysis_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    review_status: Mapped[ReviewStatus] = mapped_column(
        db_enum(ReviewStatus, 20), default=ReviewStatus.NONE
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    flags: Mapped[list["TransactionFlag"]] = relationship(
        back_populates="transaction", lazy="raise"
    )


class TransactionFlag(Base):
    __tablename__ = "transaction_flags"
    __table_args__ = (
        # One flag per reason per transaction keeps re-running analysis idempotent.
        UniqueConstraint("transaction_id", "reason", name="uq_transaction_flags_transaction_reason"),
        Index("ix_transaction_flags_reason", "reason"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("fuel_transactions.id"))
    reason: Mapped[FlagReason] = mapped_column(db_enum(FlagReason))
    details: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    transaction: Mapped[FuelTransaction] = relationship(
        back_populates="flags", lazy="raise"
    )
