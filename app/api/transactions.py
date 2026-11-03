from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageParams
from app.core.errors import AUTH_ERRORS, CONFLICT, INVALID_STATE, NOT_FOUND
from app.core.security import require_manager, require_reader, require_terminal
from app.db.async_db import get_db
from app.models.enums import FlagReason, ReviewStatus, TransactionStatus
from app.schemas.transactions import (
    PurchaseRequest,
    ReviewRequest,
    TransactionDetail,
    TransactionRead,
)
from app.services import review_service, transaction_service
from app.workers import names
from app.workers.dispatch import enqueue

router = APIRouter(prefix="/transactions", tags=["Transactions"], responses=AUTH_ERRORS)


@router.post(
    "",
    response_model=TransactionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_terminal)],
    responses={
        200: {"model": TransactionRead, "description": "Replay: this Idempotency-Key was already processed"},
        **NOT_FOUND,
        **CONFLICT,
    },
    summary="Simulate a fuel card purchase",
    description=(
        "Called by the card terminal. The card status and truck are checked right away: "
        "a card that is inactive or blocked, a driver without a truck, or a truck that does "
        "not match the driver's assignment is **declined** (`authorized: false`, with a "
        "`decline_reason`). Declined attempts are stored too.\n\n"
        "Everything else is accepted and saved as `pending`. Limits, tank capacity, "
        "unusually large purchases and bursts are checked afterwards by a background worker, "
        "which flags the transaction for review.\n\n"
        "Send a unique `Idempotency-Key` per purchase. Retrying with the same key and the "
        "same body returns the first result (200) instead of creating a duplicate."
    ),
)
async def create_purchase(
    data: PurchaseRequest,
    response: Response,
    idempotency_key: Annotated[str, Header(min_length=8, max_length=100)],
    db: AsyncSession = Depends(get_db),
):
    tx, created = await transaction_service.purchase(db, idempotency_key, data)
    if not created:
        response.status_code = status.HTTP_200_OK
        return tx

    if tx.status == TransactionStatus.DECLINED:
        await enqueue(names.CHECK_DECLINED_ATTEMPT, tx.id)
    else:
        await enqueue(names.ANALYZE_TRANSACTION, tx.id)
    return tx


@router.get(
    "",
    response_model=list[TransactionRead],
    dependencies=[Depends(require_reader)],
    summary="List transactions (newest first)",
)
async def list_transactions(
    page: PageParams,
    card_id: int | None = None,
    driver_id: int | None = None,
    truck_id: int | None = None,
    status: TransactionStatus | None = None,
    db: AsyncSession = Depends(get_db),
):
    return await transaction_service.list_transactions(
        db, card_id, driver_id, truck_id, status, page.limit, page.offset
    )


# The fixed paths below must stay above /{transaction_id}, or "flagged" is read as an id.


@router.get(
    "/flagged",
    response_model=list[TransactionDetail],
    dependencies=[Depends(require_reader)],
    summary="Suspicious transactions waiting for review",
    description=(
        "Every transaction with at least one risk flag, newest first, with the flags and "
        "the numbers behind them. Use `review_status=none` to see only the open ones."
    ),
)
async def list_flagged(
    page: PageParams,
    reason: FlagReason | None = None,
    review_status: ReviewStatus | None = None,
    card_id: int | None = None,
    driver_id: int | None = None,
    truck_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    db: AsyncSession = Depends(get_db),
):
    return await review_service.list_flagged(
        db, reason, review_status, card_id, driver_id, truck_id,
        date_from, date_to, page.limit, page.offset,
    )  # fmt: skip


@router.get(
    "/stuck",
    response_model=list[TransactionRead],
    dependencies=[Depends(require_reader)],
    summary="Transactions the background analysis has not finished",
    description=(
        "Pending transactions (and declined attempts not yet checked) older than "
        "`older_than_minutes`. The recovery job re-queues these automatically."
    ),
)
async def list_stuck(
    page: PageParams,
    older_than_minutes: Annotated[int | None, Query(ge=0)] = None,
    db: AsyncSession = Depends(get_db),
):
    return await review_service.list_stuck(db, older_than_minutes, page.limit, page.offset)


@router.get(
    "/{transaction_id}",
    response_model=TransactionDetail,
    dependencies=[Depends(require_reader)],
    responses=NOT_FOUND,
)
async def get_transaction(transaction_id: int, db: AsyncSession = Depends(get_db)):
    return await review_service.load_detail(db, transaction_id)


@router.patch(
    "/{transaction_id}/review",
    response_model=TransactionDetail,
    dependencies=[Depends(require_manager)],
    responses={**NOT_FOUND, **INVALID_STATE},
    summary="Mark a flagged transaction as reviewed or dismissed",
    description="The transaction and its flags are kept. A later review replaces the earlier one.",
)
async def review_transaction(
    transaction_id: int, data: ReviewRequest, db: AsyncSession = Depends(get_db)
):
    return await review_service.review(db, transaction_id, data.review_status, data.note)
