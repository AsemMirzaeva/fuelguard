"""Risk rules as pure functions: numbers in, flags out. No database access here."""

from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import FlagReason


@dataclass(frozen=True)
class FlagResult:
    reason: FlagReason
    details: dict


def check_limits(
    total_price: Decimal,
    spent_day: Decimal,
    spent_week: Decimal,
    spent_month: Decimal,
    per_transaction_limit: Decimal | None,
    daily_limit: Decimal | None,
    weekly_limit: Decimal | None,
    monthly_limit: Decimal | None,
) -> list[FlagResult]:
    """Spent totals are rolling windows (24 h, 7 d, 30 d) and include this purchase."""
    checks = (
        (FlagReason.EXCEEDS_TRANSACTION_LIMIT, total_price, per_transaction_limit),
        (FlagReason.EXCEEDS_DAILY_LIMIT, spent_day, daily_limit),
        (FlagReason.EXCEEDS_WEEKLY_LIMIT, spent_week, weekly_limit),
        (FlagReason.EXCEEDS_MONTHLY_LIMIT, spent_month, monthly_limit),
    )
    return [
        FlagResult(reason, {"limit": str(limit), "amount": str(amount)})
        for reason, amount, limit in checks
        if limit is not None and amount > limit
    ]


def check_tank(gallons: Decimal, tank_capacity_gal: Decimal) -> FlagResult | None:
    if gallons > tank_capacity_gal:
        return FlagResult(
            FlagReason.EXCEEDS_TANK_CAPACITY,
            {"gallons": str(gallons), "tank_capacity_gal": str(tank_capacity_gal)},
        )
    return None


def check_large(
    gallons: Decimal,
    history_count: int,
    history_avg: Decimal | None,
    tank_capacity_gal: Decimal | None,
    multiplier: float,
    min_history: int,
    fallback_tank_ratio: float,
) -> FlagResult | None:
    """Compare with the driver's own average; until there is enough history, use a share of the tank."""
    if history_count >= min_history and history_avg is not None:
        threshold = history_avg * Decimal(str(multiplier))
        basis = "driver_average"
    elif tank_capacity_gal is not None:
        threshold = tank_capacity_gal * Decimal(str(fallback_tank_ratio))
        basis = "tank_fallback"
    else:
        return None

    if gallons > threshold:
        return FlagResult(
            FlagReason.UNUSUALLY_LARGE,
            {
                "gallons": str(gallons),
                "threshold": str(threshold.quantize(Decimal("0.001"))),
                "basis": basis,
                "history_count": history_count,
            },
        )
    return None


def check_frequency(
    count: int, max_purchases: int, window_minutes: int, reason: FlagReason
) -> FlagResult | None:
    """count includes the transaction being checked."""
    if count >= max_purchases:
        return FlagResult(
            reason,
            {"count": count, "threshold": max_purchases, "window_minutes": window_minutes},
        )
    return None
