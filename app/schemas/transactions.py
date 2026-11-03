from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import DeclineReason, FlagReason, ReviewStatus, TransactionStatus

Gallons = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=3)]
Price = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class PurchaseRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    card_number: str = Field(min_length=4, max_length=32, examples=["7001-2345-6789"])
    station_id: int = Field(examples=[1])
    gallons: Gallons = Field(examples=["42.500"])
    total_price: Price = Field(examples=["161.50"])
    truck_id: int | None = Field(
        default=None,
        description="Optional. If sent, it must match the truck assigned to the card's driver.",
        examples=[1],
    )


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    idempotency_key: str
    card_id: int
    driver_id: int
    truck_id: int | None
    station_id: int
    gallons: Decimal
    total_price: Decimal
    transaction_time: datetime
    status: TransactionStatus
    decline_reason: DeclineReason | None
    review_status: ReviewStatus
    created_at: datetime

    @computed_field(description="False only when the purchase was declined at the pump.")
    @property
    def authorized(self) -> bool:
        return self.status != TransactionStatus.DECLINED


class FlagRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reason: FlagReason
    details: dict | None
    created_at: datetime


class TransactionDetail(TransactionRead):
    analysis_done_at: datetime | None
    review_note: str | None
    reviewed_at: datetime | None
    flags: list[FlagRead]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    review_status: Literal[ReviewStatus.REVIEWED, ReviewStatus.DISMISSED] = Field(
        examples=["reviewed"]
    )
    note: str | None = Field(default=None, max_length=1000, examples=["Checked with the driver"])


class StationCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120, examples=["Shell Highway 10"])
    address: str | None = Field(default=None, max_length=255, examples=["Exit 42, Dallas TX"])


class StationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    address: str | None
