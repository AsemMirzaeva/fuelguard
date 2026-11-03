from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app.db.sync_db import session_scope
from app.models.enums import FlagReason as F
from app.models.enums import TransactionStatus as S
from app.workers import risk_analysis
from tests import seed
from tests.seed import add_declined, add_tx, snapshot


def analyze(tx_id: int) -> bool:
    with session_scope() as s:
        return risk_analysis.analyze_transaction(s, tx_id)


def flags_after_analysis(tx_id: int) -> set:
    analyze(tx_id)
    return snapshot(tx_id)["flags"]


class TestCleanPurchase:
    def test_no_flags_marks_it_approved(self, db, world):
        tx = add_tx(db, world, gallons="40", price="100")
        assert analyze(tx) is False
        snap = snapshot(tx)
        assert snap["status"] == S.APPROVED and snap["done"] and snap["flags"] == set()

    def test_flagged_purchase_is_still_approved_and_kept(self, db):
        w = seed.create_world(db, per_transaction_limit=10)
        tx = add_tx(db, w, price="50")
        assert analyze(tx) is True
        snap = snapshot(tx)
        assert snap["status"] == S.APPROVED and snap["flags"] == {F.EXCEEDS_TRANSACTION_LIMIT}


class TestLimits:
    def test_exactly_at_the_limit_is_fine_one_cent_over_is_flagged(self, db):
        w = seed.create_world(db, per_transaction_limit=300)
        assert flags_after_analysis(add_tx(db, w, price="300.00")) == set()
        assert flags_after_analysis(add_tx(db, w, hours_ago=1, price="300.01")) == {F.EXCEEDS_TRANSACTION_LIMIT}

    def test_rolling_windows_ignore_purchases_outside_each_window(self, db):
        w = seed.create_world(
            db, per_transaction_limit=300, daily_limit=500, weekly_limit=1000, monthly_limit=1300
        )
        add_tx(db, w, hours_ago=5, price="250", status=S.APPROVED)   # in all windows
        add_tx(db, w, days_ago=3, price="400", status=S.APPROVED)    # week + month
        add_tx(db, w, days_ago=10, price="300", status=S.APPROVED)   # month only
        add_tx(db, w, days_ago=40, price="900", status=S.APPROVED)   # outside every window
        tx = add_tx(db, w, price="310")
        # day 560 > 500, week 960 <= 1000, month 1260 <= 1300
        assert flags_after_analysis(tx) == {F.EXCEEDS_TRANSACTION_LIMIT, F.EXCEEDS_DAILY_LIMIT}
        assert snapshot(tx)["details"][F.EXCEEDS_DAILY_LIMIT] == {"limit": "500.00", "amount": "560.00"}

    @pytest.mark.parametrize(
        "limit_field, reason, hours_ago, counted",
        [
            ("daily_limit", F.EXCEEDS_DAILY_LIMIT, 23, True),
            ("daily_limit", F.EXCEEDS_DAILY_LIMIT, 25, False),
            ("weekly_limit", F.EXCEEDS_WEEKLY_LIMIT, 6.5 * 24, True),
            ("weekly_limit", F.EXCEEDS_WEEKLY_LIMIT, 7.5 * 24, False),
            ("monthly_limit", F.EXCEEDS_MONTHLY_LIMIT, 29 * 24, True),
            ("monthly_limit", F.EXCEEDS_MONTHLY_LIMIT, 31 * 24, False),
        ],
    )
    def test_each_window_edge(self, db, limit_field, reason, hours_ago, counted):
        """A 90 purchase plus a 20 purchase breaks a limit of 100 only if the old one is inside the window."""
        w = seed.create_world(db, **{limit_field: 100})
        add_tx(db, w, hours_ago=hours_ago, price="90", status=S.APPROVED)
        flags = flags_after_analysis(add_tx(db, w, price="20"))
        assert (reason in flags) is counted

    def test_weekly_limit_can_be_hit_on_its_own(self, db):
        w = seed.create_world(db, daily_limit=500, weekly_limit=800)
        add_tx(db, w, days_ago=3, price="700", status=S.APPROVED)
        assert flags_after_analysis(add_tx(db, w, price="150")) == {F.EXCEEDS_WEEKLY_LIMIT}

    def test_monthly_limit_can_be_hit_on_its_own(self, db):
        w = seed.create_world(db, weekly_limit=800, monthly_limit=1000)
        add_tx(db, w, days_ago=20, price="900", status=S.APPROVED)
        assert flags_after_analysis(add_tx(db, w, price="150")) == {F.EXCEEDS_MONTHLY_LIMIT}

    def test_declined_attempts_do_not_count_towards_spending(self, db):
        w = seed.create_world(db, daily_limit=500)
        add_declined(db, w, hours_ago=2, price="1000")
        assert flags_after_analysis(add_tx(db, w, price="100")) == set()

    def test_earlier_pending_purchases_count_even_before_they_are_analyzed(self, db):
        w = seed.create_world(db, daily_limit=500)
        add_tx(db, w, hours_ago=1, price="400", status=S.PENDING)
        assert flags_after_analysis(add_tx(db, w, price="200")) == {F.EXCEEDS_DAILY_LIMIT}

    def test_later_purchases_do_not_count_against_an_earlier_one(self, db):
        w = seed.create_world(db, daily_limit=500)
        earlier = add_tx(db, w, hours_ago=2, price="300")
        add_tx(db, w, hours_ago=1, price="400", status=S.APPROVED)
        assert flags_after_analysis(earlier) == set()

    def test_result_does_not_depend_on_the_order_of_analysis(self, db):
        w = seed.create_world(db, daily_limit=500)
        first, second = add_tx(db, w, hours_ago=2, price="300"), add_tx(db, w, hours_ago=1, price="300")
        assert flags_after_analysis(second) == {F.EXCEEDS_DAILY_LIMIT}  # analyzed out of order
        assert flags_after_analysis(first) == set()

    def test_same_timestamp_is_ordered_by_id(self, db):
        w = seed.create_world(db, daily_limit=500)
        at = datetime.now(timezone.utc)
        a, b = add_tx(db, w, at=at, price="300"), add_tx(db, w, at=at, price="300")
        assert flags_after_analysis(a) == set()
        assert flags_after_analysis(b) == {F.EXCEEDS_DAILY_LIMIT}

    def test_limits_are_per_card(self, db):
        w = seed.create_world(db, daily_limit=500)
        other_card = seed.add_card(db, w.driver_id)
        add_tx(db, w, hours_ago=2, price="450", card_id=other_card, status=S.APPROVED)
        assert flags_after_analysis(add_tx(db, w, price="100")) == set()

    def test_card_without_limits_is_never_limit_flagged(self, db, world):
        assert flags_after_analysis(add_tx(db, world, gallons="1", price="999999")) == set()


class TestTankCapacity:
    def test_over_capacity(self, db, world):
        tx = add_tx(db, world, gallons="101")
        assert F.EXCEEDS_TANK_CAPACITY in flags_after_analysis(tx)
        assert snapshot(tx)["details"][F.EXCEEDS_TANK_CAPACITY] == {"gallons": "101.000", "tank_capacity_gal": "100.00"}

    def test_exactly_full_tank_is_not_flagged(self, db, world):
        assert F.EXCEEDS_TANK_CAPACITY not in flags_after_analysis(add_tx(db, world, gallons="100"))

    def test_uses_the_capacity_of_the_truck_on_the_transaction(self, db):
        big, small = seed.create_world(db, capacity="300"), seed.create_world(db, capacity="50")
        assert F.EXCEEDS_TANK_CAPACITY not in flags_after_analysis(add_tx(db, big, gallons="200"))
        assert F.EXCEEDS_TANK_CAPACITY in flags_after_analysis(add_tx(db, small, gallons="60"))


class TestUnusuallyLarge:
    def history(self, db, w, n=5, gallons="40", **kw):
        for i in range(n):
            add_tx(db, w, days_ago=i + 1, gallons=gallons, status=S.APPROVED, **kw)

    def test_new_driver_uses_80_percent_of_the_tank(self, db, world):
        assert flags_after_analysis(add_tx(db, world, hours_ago=1, gallons="80")) == set()
        tx = add_tx(db, world, gallons="85")
        assert F.UNUSUALLY_LARGE in flags_after_analysis(tx)
        assert snapshot(tx)["details"][F.UNUSUALLY_LARGE]["basis"] == "tank_fallback"

    def test_with_enough_history_it_compares_with_the_driver_average(self, db, world):
        self.history(db, world)  # average 40, so the limit is 80
        tx = add_tx(db, world, gallons="95")
        assert flags_after_analysis(tx) == {F.UNUSUALLY_LARGE}
        assert snapshot(tx)["details"][F.UNUSUALLY_LARGE]["basis"] == "driver_average"

    def test_four_past_purchases_are_not_enough_history(self, db, world):
        self.history(db, world, n=4, gallons="10")  # an average of 10 would flag 30 gal
        assert flags_after_analysis(add_tx(db, world, gallons="30")) == set()

    def test_a_driver_who_normally_buys_a_lot_is_not_flagged_for_it(self, db, world):
        self.history(db, world, gallons="70")  # average 70: 90 gal is below the 140 limit
        assert flags_after_analysis(add_tx(db, world, gallons="90")) == set()

    def test_declined_attempts_are_not_history(self, db, world):
        for i in range(5):
            add_declined(db, world, days_ago=i + 1, gallons="5")
        assert flags_after_analysis(add_tx(db, world, gallons="70")) == set()  # fallback 80 still applies

    def test_later_purchases_are_not_history(self, db, world):
        earlier = add_tx(db, world, days_ago=1, gallons="30")
        for i in range(5):
            add_tx(db, world, hours_ago=1 + i / 100, gallons="5", status=S.APPROVED)
        assert flags_after_analysis(earlier) == set()

    def test_history_belongs_to_the_driver_not_the_card(self, db, world):
        second_card = seed.add_card(db, world.driver_id)
        self.history(db, world, card_id=second_card)  # bought on another card of the same driver
        assert flags_after_analysis(add_tx(db, world, gallons="95")) == {F.UNUSUALLY_LARGE}

    def test_other_drivers_history_is_ignored(self, db, world):
        other = seed.create_world(db)
        self.history(db, other, gallons="90")
        tx = add_tx(db, world, gallons="85")
        assert flags_after_analysis(tx) == {F.UNUSUALLY_LARGE}
        assert snapshot(tx)["details"][F.UNUSUALLY_LARGE]["basis"] == "tank_fallback"  # no history of its own


class TestFrequency:
    def test_three_in_the_window_flag_the_third_card(self, db, world):
        add_tx(db, world, minutes_ago=20, status=S.APPROVED)
        add_tx(db, world, minutes_ago=10, status=S.APPROVED)
        tx = add_tx(db, world)
        flags = flags_after_analysis(tx)
        assert {F.CARD_FREQUENCY, F.TRUCK_FREQUENCY} <= flags
        assert snapshot(tx)["details"][F.CARD_FREQUENCY] == {"count": 3, "threshold": 3, "window_minutes": 30}

    def test_two_are_fine(self, db, world):
        add_tx(db, world, minutes_ago=10, status=S.APPROVED)
        assert flags_after_analysis(add_tx(db, world)) == set()

    def test_the_window_is_30_minutes(self, db, world):
        add_tx(db, world, minutes_ago=31, status=S.APPROVED)
        add_tx(db, world, minutes_ago=29.5, status=S.APPROVED)
        assert flags_after_analysis(add_tx(db, world)) == set()  # only 2 in the window
        add_tx(db, world, minutes_ago=5, status=S.APPROVED)
        later = add_tx(db, world)
        assert F.CARD_FREQUENCY in flags_after_analysis(later)  # 29.5, 5 and now

    def test_declined_attempts_count_towards_the_burst(self, db, world):
        add_declined(db, world, minutes_ago=20)
        add_declined(db, world, minutes_ago=10)
        assert F.CARD_FREQUENCY in flags_after_analysis(add_tx(db, world))

    def test_truck_burst_across_different_cards(self, db, world):
        _, other_card = seed.add_driver_on_truck(db, world.truck_id)
        add_tx(db, world, minutes_ago=20, status=S.APPROVED)
        add_tx(db, world, minutes_ago=10, card_id=other_card, status=S.APPROVED)
        flags = flags_after_analysis(add_tx(db, world))
        assert F.TRUCK_FREQUENCY in flags and F.CARD_FREQUENCY not in flags

    def test_other_cards_and_trucks_do_not_count(self, db, world):
        other = seed.create_world(db)
        for m in (20, 10):
            add_tx(db, other, minutes_ago=m, status=S.APPROVED)
        assert flags_after_analysis(add_tx(db, world)) == set()

    def test_later_attempts_do_not_count(self, db, world):
        first = add_tx(db, world, minutes_ago=20)
        add_tx(db, world, minutes_ago=10, status=S.APPROVED)
        add_tx(db, world, minutes_ago=5, status=S.APPROVED)
        assert flags_after_analysis(first) == set()

    def test_a_transaction_without_a_truck_only_gets_the_card_check(self, db, world):
        add_declined(db, world, minutes_ago=20, truck_id=None)
        add_declined(db, world, minutes_ago=10, truck_id=None)
        tx = add_tx(db, world, truck_id=None)
        assert flags_after_analysis(tx) == {F.CARD_FREQUENCY}


class TestSafeToRepeat:
    def test_second_run_changes_nothing(self, db):
        w = seed.create_world(db, per_transaction_limit=10)
        tx = add_tx(db, w, price="50")
        assert analyze(tx) is True
        first = snapshot(tx)
        assert analyze(tx) is False
        assert snapshot(tx) == first and first["flag_rows"] == 1

    @pytest.mark.parametrize("status", [S.APPROVED, S.DECLINED])
    def test_only_pending_transactions_are_analyzed(self, db, world, status):
        tx = add_tx(db, world, status=status, price="999999", analyzed=False)
        assert analyze(tx) is False and snapshot(tx)["status"] == status

    def test_unknown_transaction_is_ignored(self):
        assert analyze(999999) is False

    def test_parallel_workers_on_the_same_transaction_flag_it_once(self, db):
        w = seed.create_world(db, per_transaction_limit=10, daily_limit=10)
        tx = add_tx(db, w, price="50")
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: analyze(tx), range(6)))
        assert results.count(True) == 1
        assert snapshot(tx)["flag_rows"] == 2 and snapshot(tx)["status"] == S.APPROVED


class TestDeclinedAttempts:
    def check(self, tx_id):
        with session_scope() as s:
            return risk_analysis.check_declined_attempt(s, tx_id)

    def test_third_attempt_in_the_window_is_flagged_earlier_ones_are_not(self, db, world):
        first, second, third = (add_declined(db, world, minutes_ago=m) for m in (20, 10, 0))
        assert [self.check(t) for t in (first, second, third)] == [False, False, True]
        assert snapshot(third)["flags"] == {F.CARD_FREQUENCY, F.TRUCK_FREQUENCY}
        assert all(snapshot(t)["done"] for t in (first, second, third))

    def test_attempts_without_a_truck_only_get_the_card_check(self, db, world):
        txs = [add_declined(db, world, minutes_ago=m, truck_id=None) for m in (20, 10, 0)]
        assert self.check(txs[-1]) is True and snapshot(txs[-1])["flags"] == {F.CARD_FREQUENCY}

    def test_approved_purchases_in_the_window_count_too(self, db, world):
        add_tx(db, world, minutes_ago=20, status=S.APPROVED)
        add_tx(db, world, minutes_ago=10, status=S.APPROVED)
        assert self.check(add_declined(db, world)) is True

    def test_second_run_does_nothing(self, db, world):
        txs = [add_declined(db, world, minutes_ago=m) for m in (20, 10, 0)]
        assert self.check(txs[-1]) is True
        assert self.check(txs[-1]) is False and snapshot(txs[-1])["flag_rows"] == 2

    def test_only_declined_transactions_are_checked(self, db, world):
        tx = add_tx(db, world, status=S.APPROVED, analyzed=False)
        assert self.check(tx) is False and snapshot(tx)["done"] is False

    def test_unknown_transaction(self):
        assert self.check(999999) is False


class TestNotifications:
    def create(self, tx_id):
        with session_scope() as s:
            return risk_analysis.create_notification(s, tx_id)

    def notifications(self, tx_id):
        from app.models import Notification

        with session_scope() as s:
            return [n.message for n in s.query(Notification).filter_by(transaction_id=tx_id)]

    def test_message_names_the_reasons_card_and_driver(self, db, world):
        tx = add_tx(db, world, gallons="12.5", price="40", status=S.APPROVED)
        seed.add_flag(db, tx, F.EXCEEDS_DAILY_LIMIT)
        seed.add_flag(db, tx, F.UNUSUALLY_LARGE)
        assert self.create(tx) is True
        (message,) = self.notifications(tx)
        for part in (f"#{tx}", "exceeds_daily_limit, unusually_large", world.card_number, "Driver", "12.500", "40.00"):
            assert part in message

    def test_only_one_notification_per_transaction(self, db, world):
        tx = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, tx)
        assert self.create(tx) is True and self.create(tx) is False
        assert len(self.notifications(tx)) == 1

    def test_no_flags_no_notification(self, db, world):
        tx = add_tx(db, world, status=S.APPROVED)
        assert self.create(tx) is False and self.notifications(tx) == []

    def test_unknown_transaction(self):
        assert self.create(999999) is False

    def test_declined_attempts_can_raise_alerts_too(self, db, world):
        tx = add_declined(db, world)
        seed.add_flag(db, tx, F.CARD_FREQUENCY)
        assert self.create(tx) is True and "(declined)" in self.notifications(tx)[0]

    def test_parallel_workers_create_one_notification(self, db, world):
        tx = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, tx)
        with ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: self.create(tx), range(6)))
        assert results.count(True) == 1 and len(self.notifications(tx)) == 1


class TestDailyReport:
    DAY = datetime(2026, 2, 10, tzinfo=timezone.utc).date()

    def at(self, h=12, m=0, s=0, day=10):
        return datetime(2026, 2, day, h, m, s, tzinfo=timezone.utc)

    def build(self):
        with session_scope() as s:
            return risk_analysis.build_daily_report(s, self.DAY)

    def rows(self):
        from app.models import DailyReport

        with session_scope() as s:
            return {
                r.card_id: (r.total_gallons, r.total_spend, r.transaction_count, r.flagged_count)
                for r in s.query(DailyReport).filter_by(report_date=self.DAY)
            }

    def test_totals_per_card_with_flagged_count(self, db, world):
        other = seed.create_world(db)
        a = add_tx(db, world, at=self.at(8), gallons="10", price="30", status=S.APPROVED)
        add_tx(db, world, at=self.at(9), gallons="20.5", price="70.25", status=S.PENDING)
        add_tx(db, other, at=self.at(10), gallons="5", price="15", status=S.APPROVED)
        seed.add_flag(db, a)
        assert self.build() == 2
        rows = self.rows()
        assert rows[world.card_id] == (30.5, 100.25, 2, 1) and rows[other.card_id] == (5, 15, 1, 0)

    def test_declined_attempts_are_not_spending(self, db, world):
        add_tx(db, world, at=self.at(8), price="30", status=S.APPROVED)
        add_declined(db, world, at=self.at(9), price="500")
        assert self.build() == 1 and self.rows()[world.card_id][1:3] == (30, 1)

    def test_day_boundaries_are_utc_and_half_open(self, db, world):
        add_tx(db, world, at=self.at(0, 0, 0), price="1", status=S.APPROVED)              # included
        add_tx(db, world, at=self.at(23, 59, 59), price="2", status=S.APPROVED)           # included
        add_tx(db, world, at=self.at(23, 59, 59, day=9), price="100", status=S.APPROVED)  # day before
        add_tx(db, world, at=self.at(0, 0, 0, day=11), price="200", status=S.APPROVED)    # day after
        self.build()
        assert self.rows()[world.card_id][1:3] == (3, 2)

    def test_rerun_updates_the_row_instead_of_duplicating_it(self, db, world):
        add_tx(db, world, at=self.at(8), price="30", status=S.APPROVED)
        self.build()
        add_tx(db, world, at=self.at(9), price="20", status=S.APPROVED)
        assert self.build() == 1
        assert self.rows()[world.card_id][1:3] == (50, 2)

    def test_empty_day_creates_no_rows(self, db, world):
        add_tx(db, world, at=self.at(8, day=9), status=S.APPROVED)
        assert self.build() == 0 and self.rows() == {}


class TestFindStuck:
    def find(self):
        with session_scope() as s:
            unfinished, unnotified = risk_analysis.find_stuck(s)
        return [i for i, _ in unfinished], unnotified

    def test_finds_old_pending_and_unchecked_declined(self, db, world):
        pending = add_tx(db, world, created_minutes_ago=10)
        declined = add_declined(db, world, created_minutes_ago=10)
        assert self.find() == ([pending, declined], [])

    def test_ignores_recent_and_finished_work(self, db, world):
        add_tx(db, world, created_minutes_ago=1)
        add_tx(db, world, created_minutes_ago=60, status=S.APPROVED)
        add_declined(db, world, created_minutes_ago=60, analyzed=True)
        assert self.find() == ([], [])

    def test_finds_flagged_transactions_that_never_got_a_notification(self, db, world):
        missing = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, missing, minutes_ago=10)
        done = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, done, minutes_ago=10)
        seed.add_notification(db, done)
        fresh = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, fresh, minutes_ago=1)
        assert self.find() == ([], [missing])

    def test_a_transaction_with_two_flags_is_listed_once(self, db, world):
        tx = add_tx(db, world, status=S.APPROVED)
        seed.add_flag(db, tx, F.EXCEEDS_DAILY_LIMIT, minutes_ago=10)
        seed.add_flag(db, tx, F.UNUSUALLY_LARGE, minutes_ago=10)
        assert self.find() == ([], [tx])
