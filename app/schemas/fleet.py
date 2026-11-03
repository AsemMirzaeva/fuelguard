from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

TankGallons = Annotated[Decimal, Field(gt=0, max_digits=8, decimal_places=2)]


class _Base(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


def _reject_null(model: BaseModel, fields: tuple[str, ...]) -> None:
    for name in fields:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null")


class DriverCreate(_Base):
    full_name: str = Field(min_length=1, max_length=120, examples=["John Smith"])
    license_number: str = Field(min_length=3, max_length=50, examples=["TX-1234567"])
    phone: str | None = Field(default=None, max_length=30, examples=["+1 555 0100"])


class DriverUpdate(_Base):
    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    license_number: str | None = Field(default=None, min_length=3, max_length=50)
    phone: str | None = Field(default=None, max_length=30)

    @model_validator(mode="after")
    def _no_nulls(self):
        _reject_null(self, ("full_name", "license_number"))
        return self


class DriverRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    license_number: str
    phone: str | None
    is_active: bool
    created_at: datetime
    current_truck_id: int | None = None


class TruckCreate(_Base):
    plate_number: str = Field(min_length=2, max_length=20, examples=["TX-4821"])
    model: str | None = Field(default=None, max_length=80, examples=["Volvo VNL 860"])
    tank_capacity_gal: TankGallons = Field(examples=["150.00"])


class TruckUpdate(_Base):
    plate_number: str | None = Field(default=None, min_length=2, max_length=20)
    model: str | None = Field(default=None, max_length=80)
    tank_capacity_gal: TankGallons | None = None

    @model_validator(mode="after")
    def _no_nulls(self):
        _reject_null(self, ("plate_number", "tank_capacity_gal"))
        return self


class TruckRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plate_number: str
    model: str | None
    tank_capacity_gal: Decimal
    is_active: bool
    created_at: datetime


class AssignTruckRequest(BaseModel):
    truck_id: int = Field(examples=[1])


class AssignmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    driver_id: int
    truck_id: int
    assigned_at: datetime
    unassigned_at: datetime | None
