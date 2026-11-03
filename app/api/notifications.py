from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageParams
from app.core.errors import AUTH_ERRORS, NOT_FOUND
from app.core.security import require_manager
from app.db.async_db import get_db
from app.schemas.reporting import NotificationRead
from app.services import report_service

router = APIRouter(
    prefix="/notifications",
    tags=["Notifications"],
    dependencies=[Depends(require_manager)],
    responses=AUTH_ERRORS,
)


@router.get(
    "",
    response_model=list[NotificationRead],
    summary="Alerts about suspicious transactions (newest first)",
)
async def list_notifications(
    page: PageParams, unread: bool | None = None, db: AsyncSession = Depends(get_db)
):
    return await report_service.list_notifications(db, unread, page.limit, page.offset)


@router.patch("/{notification_id}/read", response_model=NotificationRead, responses=NOT_FOUND)
async def mark_read(notification_id: int, db: AsyncSession = Depends(get_db)):
    return await report_service.mark_read(db, notification_id)
