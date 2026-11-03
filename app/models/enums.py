import enum

from sqlalchemy import Enum


def db_enum(enum_cls: type[enum.Enum], length: int = 40) -> Enum:
    # Stored as VARCHAR so adding a value never needs an ALTER TYPE migration.
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        values_callable=lambda e: [m.value for m in e],
    )


class CardStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"


class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"


class DeclineReason(str, enum.Enum):
    CARD_INACTIVE = "card_inactive"
    CARD_BLOCKED = "card_blocked"
    NO_VEHICLE = "no_vehicle"
    VEHICLE_MISMATCH = "vehicle_mismatch"


class ReviewStatus(str, enum.Enum):
    NONE = "none"
    REVIEWED = "reviewed"
    DISMISSED = "dismissed"


class FlagReason(str, enum.Enum):
    EXCEEDS_TRANSACTION_LIMIT = "exceeds_transaction_limit"
    EXCEEDS_DAILY_LIMIT = "exceeds_daily_limit"
    EXCEEDS_WEEKLY_LIMIT = "exceeds_weekly_limit"
    EXCEEDS_MONTHLY_LIMIT = "exceeds_monthly_limit"
    EXCEEDS_TANK_CAPACITY = "exceeds_tank_capacity"
    UNUSUALLY_LARGE = "unusually_large"
    CARD_FREQUENCY = "card_frequency"
    TRUCK_FREQUENCY = "truck_frequency"
