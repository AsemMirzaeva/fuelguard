from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, InvalidStateError
from app.models import CardStatusHistory, Driver, FuelCard
from app.models.enums import CardStatus
from app.schemas.cards import CardCreate, CardLimitsUpdate
from app.services.common import commit_or_conflict, get_or_404


async def create_card(db: AsyncSession, data: CardCreate, changed_by: str) -> FuelCard:
    driver = await get_or_404(db, Driver, data.driver_id, "Driver")
    if not driver.is_active:
        raise InvalidStateError("Cannot issue a card to an inactive driver")

    card = FuelCard(**data.model_dump(), status=CardStatus.ACTIVE)
    async with commit_or_conflict(db, "A card with this number already exists"):
        db.add(card)
        await db.flush()
        db.add(
            CardStatusHistory(
                card_id=card.id, old_status=None, new_status=CardStatus.ACTIVE, changed_by=changed_by
            )
        )
    await db.refresh(card)
    return card


async def list_cards(
    db: AsyncSession,
    driver_id: int | None,
    status: CardStatus | None,
    limit: int,
    offset: int,
) -> list[FuelCard]:
    stmt = select(FuelCard).order_by(FuelCard.id).limit(limit).offset(offset)
    if driver_id is not None:
        stmt = stmt.where(FuelCard.driver_id == driver_id)
    if status is not None:
        stmt = stmt.where(FuelCard.status == status)
    return list((await db.execute(stmt)).scalars())


async def get_card(db: AsyncSession, card_id: int) -> FuelCard:
    return await get_or_404(db, FuelCard, card_id, "Card")


async def change_status(
    db: AsyncSession, card_id: int, new_status: CardStatus, changed_by: str
) -> FuelCard:
    card = await get_or_404(db, FuelCard, card_id, "Card", lock=True)
    if card.status == new_status:
        raise ConflictError(f"Card is already {new_status.value}")
    if new_status == CardStatus.ACTIVE:
        driver = await get_or_404(db, Driver, card.driver_id, "Driver")
        if not driver.is_active:
            raise InvalidStateError("Cannot activate a card of an inactive driver")

    async with commit_or_conflict(db, "Could not change card status"):
        db.add(
            CardStatusHistory(
                card_id=card.id, old_status=card.status, new_status=new_status, changed_by=changed_by
            )
        )
        card.status = new_status
    return card


async def update_limits(db: AsyncSession, card_id: int, data: CardLimitsUpdate) -> FuelCard:
    card = await get_or_404(db, FuelCard, card_id, "Card", lock=True)
    for field in data.model_fields_set:
        setattr(card, field, getattr(data, field))
    await db.commit()
    await db.refresh(card)
    return card


async def list_history(db: AsyncSession, card_id: int) -> list[CardStatusHistory]:
    await get_or_404(db, FuelCard, card_id, "Card")
    stmt = (
        select(CardStatusHistory)
        .where(CardStatusHistory.card_id == card_id)
        .order_by(CardStatusHistory.id)
    )
    return list((await db.execute(stmt)).scalars())


async def deactivate_cards_of_driver(db: AsyncSession, driver_id: int) -> None:
    """Switch off a driver's active cards. Blocked cards stay blocked. Caller commits."""
    stmt = (
        select(FuelCard)
        .where(FuelCard.driver_id == driver_id, FuelCard.status == CardStatus.ACTIVE)
        .with_for_update()
    )
    for card in (await db.execute(stmt)).scalars():
        db.add(
            CardStatusHistory(
                card_id=card.id,
                old_status=CardStatus.ACTIVE,
                new_status=CardStatus.INACTIVE,
                changed_by="system:driver_deactivated",
            )
        )
        card.status = CardStatus.INACTIVE
