from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transaction_id: int
    message: str
    created_at: datetime
    read_at: datetime | None


class DailyReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    report_date: date
    card_id: int
    total_gallons: Decimal
    total_spend: Decimal
    transaction_count: int
    flagged_count: int


class ReportRunResponse(BaseModel):
    queued: bool
    report_date: date
