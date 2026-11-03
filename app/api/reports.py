from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageParams
from app.core.errors import AUTH_ERRORS
from app.core.security import require_manager, require_reader
from app.db.async_db import get_db
from app.schemas.reporting import DailyReportRead, ReportRunResponse
from app.services import report_service
from app.workers import names
from app.workers.dispatch import enqueue

router = APIRouter(
    prefix="/reports",
    tags=["Reports"],
    dependencies=[Depends(require_reader)],
    responses=AUTH_ERRORS,
)


@router.get(
    "/daily",
    response_model=list[DailyReportRead],
    summary="Daily fuel expense report, one row per card",
    description="Built every night at 00:10 UTC for the previous day.",
)
async def list_daily_reports(
    page: PageParams,
    report_date: date | None = None,
    card_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    return await report_service.list_daily_reports(db, report_date, card_id, page.limit, page.offset)


@router.post(
    "/daily/run",
    response_model=ReportRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Build a daily report now",
    description="Queues the report job for a UTC day (default: yesterday). Re-running updates the rows.",
    dependencies=[Depends(require_manager)],
)
async def run_daily_report(report_date: date | None = None):
    day = report_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
    if not await enqueue(names.DAILY_REPORT, day.isoformat()):
        raise HTTPException(status_code=503, detail="Job queue is not available")
    return ReportRunResponse(queued=True, report_date=day)
