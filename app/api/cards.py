from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageParams
from app.core.errors import AUTH_ERRORS, CONFLICT, INVALID_STATE, NOT_FOUND
from app.core.security import require_manager
from app.db.async_db import get_db
from app.models.enums import CardStatus
from app.schemas.cards import (
    CardCreate,
    CardLimitsUpdate,
    CardRead,
    CardStatusHistoryRead,
    CardStatusUpdate,
)
from app.services import card_service

router = APIRouter(
    prefix="/cards",
    tags=["Cards"],
    dependencies=[Depends(require_manager)],
    responses=AUTH_ERRORS,
)


@router.post(
    "",
    response_model=CardRead,
    status_code=status.HTTP_201_CREATED,
    responses={**NOT_FOUND, **CONFLICT, **INVALID_STATE},
    summary="Issue a card to a driver",
    description="New cards start as active. Limits are optional; a missing limit is not checked.",
)
async def create_card(data: CardCreate, db: AsyncSession = Depends(get_db)):
    return await card_service.create_card(db, data)


@router.get("", response_model=list[CardRead])
async def list_cards(
    page: PageParams,
    driver_id: int | None = None,
    status: CardStatus | None = None,
    db: AsyncSession = Depends(get_db),
):
    return await card_service.list_cards(db, driver_id, status, page.limit, page.offset)


@router.get("/{card_id}", response_model=CardRead, responses=NOT_FOUND)
async def get_card(card_id: int, db: AsyncSession = Depends(get_db)):
    return await card_service.get_card(db, card_id)


@router.patch(
    "/{card_id}/status",
    response_model=CardRead,
    responses={**NOT_FOUND, **CONFLICT},
    summary="Activate, deactivate or block a card",
    description=(
        "Any status can change to any other, including lifting a block. "
        "Only active cards can make purchases. Every change is written to the card's history."
    ),
)
async def change_status(
    card_id: int, data: CardStatusUpdate, db: AsyncSession = Depends(get_db)
):
    return await card_service.change_status(db, card_id, data.status)


@router.patch(
    "/{card_id}/limits",
    response_model=CardRead,
    responses=NOT_FOUND,
    summary="Configure spending limits",
    description="Send only the limits to change. An explicit null removes that limit.",
)
async def update_limits(
    card_id: int, data: CardLimitsUpdate, db: AsyncSession = Depends(get_db)
):
    return await card_service.update_limits(db, card_id, data)


@router.get(
    "/{card_id}/history",
    response_model=list[CardStatusHistoryRead],
    responses=NOT_FOUND,
    summary="Status change history of a card",
)
async def list_history(card_id: int, db: AsyncSession = Depends(get_db)):
    return await card_service.list_history(db, card_id)
