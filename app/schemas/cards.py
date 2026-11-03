from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import CardStatus

Money = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]


class CardCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    card_number: str = Field(min_length=4, max_length=32, examples=["7001-2345-6789"])
    driver_id: int = Field(examples=[1])
    per_transaction_limit: Money | None = Field(default=None, examples=["300.00"])
    daily_limit: Money | None = Field(default=None, examples=["500.00"])
    weekly_limit: Money | None = Field(default=None, examples=["2500.00"])
    monthly_limit: Money | None = Field(default=None, examples=["8000.00"])


class CardLimitsUpdate(BaseModel):
    """Send only the limits to change. An explicit null removes that limit."""

    per_transaction_limit: Money | None = None
    daily_limit: Money | None = None
    weekly_limit: Money | None = None
    monthly_limit: Money | None = None


class CardStatusUpdate(BaseModel):
    status: CardStatus


class CardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    card_number: str
    driver_id: int
    status: CardStatus
    per_transaction_limit: Decimal | None
    daily_limit: Decimal | None
    weekly_limit: Decimal | None
    monthly_limit: Decimal | None
    created_at: datetime


class CardStatusHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    card_id: int
    old_status: CardStatus | None
    new_status: CardStatus
    changed_by: str
    changed_at: datetime
