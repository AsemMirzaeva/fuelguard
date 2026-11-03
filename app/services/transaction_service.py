import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import FuelCard, FuelStation, FuelTransaction, Truck
from app.models.enums import CardStatus, DeclineReason, TransactionStatus
from app.schemas.transactions import PurchaseRequest
from app.services import fleet_service
from app.services.common import get_or_404

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    status: TransactionStatus
    decline_reason: DeclineReason | None
    truck_id: int | None


async def _find_by_key(db: AsyncSession, key: str) -> FuelTransaction | None:
    stmt = select(FuelTransaction).where(FuelTransaction.idempotency_key == key)
    return (await db.execute(stmt)).scalar_one_or_none()


def _replay(existing: FuelTransaction, card_id: int, data: PurchaseRequest) -> FuelTransaction:
    """Same key + same request returns the first result. Same key + other request is an error."""
    same = (
        existing.card_id == card_id
        and existing.station_id == data.station_id
        and existing.gallons == data.gallons
        and existing.total_price == data.total_price
    )
    if not same:
        raise ConflictError("Idempotency-Key was already used with a different request")
    return existing


async def _decide(db: AsyncSession, card: FuelCard, requested_truck_id: int | None) -> Decision:
    """Instant checks, in a fixed order: card status, then truck presence, then truck match."""
    assigned = await fleet_service.current_truck_id(db, card.driver_id)

    if card.status == CardStatus.INACTIVE:
        return Decision(TransactionStatus.DECLINED, DeclineReason.CARD_INACTIVE, assigned)
    if card.status == CardStatus.BLOCKED:
        return Decision(TransactionStatus.DECLINED, DeclineReason.CARD_BLOCKED, assigned)

    if assigned is None:
        return Decision(TransactionStatus.DECLINED, DeclineReason.NO_VEHICLE, None)

    if requested_truck_id is not None and requested_truck_id != assigned:
        # Record the truck that was really at the pump, if we know it.
        known = await db.get(Truck, requested_truck_id)
        return Decision(
            TransactionStatus.DECLINED,
            DeclineReason.VEHICLE_MISMATCH,
            known.id if known else None,
        )

    return Decision(TransactionStatus.PENDING, None, assigned)


async def purchase(
    db: AsyncSession, idempotency_key: str, data: PurchaseRequest
) -> tuple[FuelTransaction, bool]:
    """Returns (transaction, created). created is False when the key was seen before."""
    stmt = select(FuelCard).where(FuelCard.card_number == data.card_number)
    card = (await db.execute(stmt)).scalar_one_or_none()
    if card is None:
        raise NotFoundError(f"Card {data.card_number} not found")

    # A rollback expires loaded objects, so keep plain values for the race path below.
    card_id, driver_id = card.id, card.driver_id

    existing = await _find_by_key(db, idempotency_key)
    if existing:
        return _replay(existing, card_id, data), False

    await get_or_404(db, FuelStation, data.station_id, "Station")
    decision = await _decide(db, card, data.truck_id)

    tx = FuelTransaction(
        idempotency_key=idempotency_key,
        card_id=card_id,
        driver_id=driver_id,
        truck_id=decision.truck_id,
        station_id=data.station_id,
        gallons=data.gallons,
        total_price=data.total_price,
        status=decision.status,
        decline_reason=decision.decline_reason,
    )
    db.add(tx)
    try:
        await db.commit()
    except IntegrityError:
        # A parallel request with the same key won the race: return its result.
        await db.rollback()
        existing = await _find_by_key(db, idempotency_key)
        if existing is None:
            raise
        return _replay(existing, card_id, data), False

    await db.refresh(tx)
    logger.info(
        "transaction %s %s reason=%s card=%s driver=%s truck=%s",
        tx.id,
        tx.status.value,
        tx.decline_reason.value if tx.decline_reason else "-",
        card_id,
        driver_id,
        tx.truck_id,
    )
    return tx, True


async def list_transactions(
    db: AsyncSession,
    card_id: int | None,
    driver_id: int | None,
    truck_id: int | None,
    status: TransactionStatus | None,
    limit: int,
    offset: int,
) -> list[FuelTransaction]:
    stmt = select(FuelTransaction).order_by(FuelTransaction.id.desc()).limit(limit).offset(offset)
    for column, value in (
        (FuelTransaction.card_id, card_id),
        (FuelTransaction.driver_id, driver_id),
        (FuelTransaction.truck_id, truck_id),
        (FuelTransaction.status, status),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    return list((await db.execute(stmt)).scalars())
