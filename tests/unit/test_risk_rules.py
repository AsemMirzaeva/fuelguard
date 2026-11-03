from decimal import Decimal as D

import pytest

from app.models.enums import FlagReason
from app.services import risk_rules as rules


def limits(**over):
    args = dict(
        total_price=D("100"),
        spent_day=D("100"),
        spent_week=D("100"),
        spent_month=D("100"),
        per_transaction_limit=None,
        daily_limit=None,
        weekly_limit=None,
        monthly_limit=None,
    )
    args.update(over)
    return rules.check_limits(**args)


def reasons(results):
    return [r.reason for r in results]


class TestCheckLimits:
    def test_no_limits_set_means_no_flags(self):
        assert limits(total_price=D("999999"), spent_day=D("999999")) == []

    def test_under_all_limits(self):
        assert limits(
            per_transaction_limit=D("300"),
            daily_limit=D("500"),
            weekly_limit=D("1000"),
            monthly_limit=D("2000"),
        ) == []

    def test_exactly_at_the_limit_is_not_flagged(self):
        assert limits(per_transaction_limit=D("100"), daily_limit=D("100")) == []

    @pytest.mark.parametrize(
        "field, reason",
        [
            ("per_transaction_limit", FlagReason.EXCEEDS_TRANSACTION_LIMIT),
            ("daily_limit", FlagReason.EXCEEDS_DAILY_LIMIT),
            ("weekly_limit", FlagReason.EXCEEDS_WEEKLY_LIMIT),
            ("monthly_limit", FlagReason.EXCEEDS_MONTHLY_LIMIT),
        ],
    )
    def test_each_limit_flags_on_its_own(self, field, reason):
        result = limits(**{field: D("99.99")})
        assert reasons(result) == [reason]

    def test_details_show_limit_and_amount(self):
        (flag,) = limits(daily_limit=D("50"), spent_day=D("120.50"))
        assert flag.details == {"limit": "50", "amount": "120.50"}

    def test_several_limits_flag_together(self):
        result = limits(per_transaction_limit=D("1"), daily_limit=D("1"), monthly_limit=D("1"))
        assert reasons(result) == [
            FlagReason.EXCEEDS_TRANSACTION_LIMIT,
            FlagReason.EXCEEDS_DAILY_LIMIT,
            FlagReason.EXCEEDS_MONTHLY_LIMIT,
        ]


class TestCheckTank:
    def test_fits(self):
        assert rules.check_tank(D("99.9"), D("100")) is None

    def test_exactly_full_tank_is_fine(self):
        assert rules.check_tank(D("100"), D("100")) is None

    def test_over_capacity(self):
        flag = rules.check_tank(D("100.001"), D("100"))
        assert flag.reason == FlagReason.EXCEEDS_TANK_CAPACITY
        assert flag.details == {"gallons": "100.001", "tank_capacity_gal": "100"}


def large(gallons, count=0, avg=None, capacity=D("100"), min_history=5):
    return rules.check_large(
        D(str(gallons)),
        count,
        avg,
        capacity,
        multiplier=2.0,
        min_history=min_history,
        fallback_tank_ratio=0.8,
    )


class TestCheckLarge:
    def test_new_driver_uses_share_of_tank(self):
        flag = large(81, count=0)
        assert flag.reason == FlagReason.UNUSUALLY_LARGE
        assert flag.details["basis"] == "tank_fallback"
        assert flag.details["threshold"] == "80.000"

    def test_fallback_threshold_itself_is_not_flagged(self):
        assert large(80, count=2, avg=D("10")) is None

    def test_enough_history_uses_driver_average(self):
        flag = large(81, count=5, avg=D("40"))
        assert flag.details["basis"] == "driver_average"
        assert flag.details["threshold"] == "80.000"
        assert flag.details["history_count"] == 5

    def test_history_just_below_minimum_still_uses_fallback(self):
        assert large(60, count=4, avg=D("10")) is None  # average would flag, fallback does not

    def test_average_can_be_stricter_than_fallback(self):
        assert large(50, count=5, avg=D("20")) is not None  # 50 > 40 although 50 < 80

    def test_average_can_be_looser_than_fallback(self):
        assert large(90, count=5, avg=D("60")) is None  # 90 < 120 although 90 > 80

    def test_without_capacity_and_history_nothing_to_compare(self):
        assert large(500, count=0, capacity=None) is None

    def test_without_capacity_but_with_history_still_works(self):
        assert large(500, count=5, avg=D("40"), capacity=None) is not None


class TestCheckFrequency:
    def test_below_threshold(self):
        assert rules.check_frequency(2, 3, 30, FlagReason.CARD_FREQUENCY) is None

    def test_at_threshold_flags(self):
        flag = rules.check_frequency(3, 3, 30, FlagReason.TRUCK_FREQUENCY)
        assert flag.reason == FlagReason.TRUCK_FREQUENCY
        assert flag.details == {"count": 3, "threshold": 3, "window_minutes": 30}

    def test_above_threshold_reports_real_count(self):
        assert rules.check_frequency(9, 3, 30, FlagReason.CARD_FREQUENCY).details["count"] == 9
