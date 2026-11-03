"""Direct database helpers: they can create backdated history, which the API cannot
(the server sets the transaction time)."""

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db.sync_db import session_scope
from app.models import (
    Driver,
    DriverTruckAssignment,
    FuelCard,
    FuelStation,
    FuelTransaction,
    Notification,
    Truck,
    TransactionFlag,
)
from app.models.enums import (
    CardStatus,
    DeclineReason,
    FlagReason,
    TransactionStatus,
)

_n = itertools.count(1)


@dataclass
class World:
    station_id: int
    driver_id: int
    truck_id: int
    card_id: int
    card_number: str


def now() -> datetime:
    return datetime.now(timezone.utc)


def create_world(session, capacity="100", **card_limits) -> World:
    n = next(_n)
    station = FuelStation(name=f"Station {n}")
    driver = Driver(full_name=f"Driver {n}", license_number=f"LIC-{n}")
    truck = Truck(plate_number=f"PL-{n}", tank_capacity_gal=Decimal(capacity))
    session.add_all([station, driver, truck])
    session.flush()
    session.add(DriverTruckAssignment(driver_id=driver.id, truck_id=truck.id))
    card = FuelCard(
        card_number=f"CARD-{n}", driver_id=driver.id, status=CardStatus.ACTIVE, **card_limits
    )
    session.add(card)
    session.commit()
    return World(station.id, driver.id, truck.id, card.id, card.card_number)


def add_card(session, driver_id: int, status=CardStatus.ACTIVE) -> int:
    card = FuelCard(card_number=f"CARD-X{next(_n)}", driver_id=driver_id, status=status)
    session.add(card)
    session.commit()
    return card.id


def add_driver_on_truck(session, truck_id: int) -> tuple[int, int]:
    """A second driver sharing a truck, with a card. Returns (driver_id, card_id)."""
    n = next(_n)
    driver = Driver(full_name=f"Driver {n}", license_number=f"LIC-{n}")
    session.add(driver)
    session.flush()
    session.add(DriverTruckAssignment(driver_id=driver.id, truck_id=truck_id))
    session.commit()
    return driver.id, add_card(session, driver.id)


def add_tx(
    session,
    w: World,
    *,
    minutes_ago: float = 0,
    hours_ago: float = 0,
    days_ago: float = 0,
    gallons="10",
    price="30",
    status: TransactionStatus = TransactionStatus.PENDING,
    decline_reason: DeclineReason | None = None,
    card_id: int | None = None,
    driver_id: int | None = None,
    truck_id: int | None = ...,
    created_minutes_ago: float = 0,
    analyzed: bool | None = None,
    at: datetime | None = None,
) -> int:
    when = at or now() - timedelta(minutes=minutes_ago, hours=hours_ago, days=days_ago)
    if analyzed is None:
        analyzed = status == TransactionStatus.APPROVED
    tx = FuelTransaction(
        idempotency_key=f"seed-{next(_n)}-{when.timestamp()}",
        card_id=card_id or w.card_id,
        driver_id=driver_id or w.driver_id,
        truck_id=w.truck_id if truck_id is ... else truck_id,
        station_id=w.station_id,
        gallons=Decimal(str(gallons)),
        total_price=Decimal(str(price)),
        transaction_time=when,
        created_at=now() - timedelta(minutes=created_minutes_ago),
        status=status,
        decline_reason=decline_reason,
        analysis_done_at=when if analyzed else None,
    )
    session.add(tx)
    session.commit()
    return tx.id


def add_declined(session, w: World, **kw) -> int:
    kw.setdefault("decline_reason", DeclineReason.CARD_BLOCKED)
    return add_tx(session, w, status=TransactionStatus.DECLINED, **kw)


def add_flag(session, tx_id: int, reason=FlagReason.EXCEEDS_DAILY_LIMIT, minutes_ago: float = 0):
    session.add(
        TransactionFlag(
            transaction_id=tx_id,
            reason=reason,
            details={"x": 1},
            created_at=now() - timedelta(minutes=minutes_ago),
        )
    )
    session.commit()


def add_notification(session, tx_id: int, message="alert") -> int:
    n = Notification(transaction_id=tx_id, message=message)
    session.add(n)
    session.commit()
    return n.id


def snapshot(tx_id: int) -> dict:
    """Fresh read of a transaction's analysis state."""
    with session_scope() as s:
        tx = s.get(FuelTransaction, tx_id)
        flags = s.query(TransactionFlag).filter_by(transaction_id=tx_id).all()
        return {
            "status": tx.status,
            "done": tx.analysis_done_at is not None,
            "flags": {f.reason for f in flags},
            "flag_rows": len(flags),
            "details": {f.reason: f.details for f in flags},
        }
