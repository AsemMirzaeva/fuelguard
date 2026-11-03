from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import DailyReport
from app.models.enums import FlagReason, TransactionStatus
from app.workers import names
from tests import seed


def ids(response) -> list[int]:
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()]


@pytest.fixture
def world(db):
    return seed.create_world(db)


def flagged(db, w, reason=FlagReason.EXCEEDS_DAILY_LIMIT, **tx_kw) -> int:
    tx_id = seed.add_tx(db, w, status=TransactionStatus.APPROVED, **tx_kw)
    seed.add_flag(db, tx_id, reason)
    return tx_id


class TestFlaggedList:
    async def test_lists_only_flagged_transactions_with_their_flags(self, manager, db, world):
        clean = seed.add_tx(db, world, status=TransactionStatus.APPROVED)
        bad = flagged(db, world)
        seed.add_flag(db, bad, FlagReason.UNUSUALLY_LARGE)
        rows = (await manager.get("/transactions/flagged")).json()
        assert [r["id"] for r in rows] == [bad] and clean not in [r["id"] for r in rows]
        assert {f["reason"] for f in rows[0]["flags"]} == {"exceeds_daily_limit", "unusually_large"}
        assert rows[0]["flags"][0]["details"] == {"x": 1}

    async def test_newest_first_and_paging(self, manager, db, world):
        a, b, c = (flagged(db, world) for _ in range(3))
        assert ids(await manager.get("/transactions/flagged")) == [c, b, a]
        assert ids(await manager.get("/transactions/flagged", params={"limit": 1, "offset": 1})) == [b]

    async def test_filter_by_reason_still_shows_all_flags(self, manager, db, world):
        a = flagged(db, world, FlagReason.EXCEEDS_DAILY_LIMIT)
        b = flagged(db, world, FlagReason.CARD_FREQUENCY)
        seed.add_flag(db, b, FlagReason.TRUCK_FREQUENCY)
        r = await manager.get("/transactions/flagged", params={"reason": "card_frequency"})
        assert ids(r) == [b] and len(r.json()[0]["flags"]) == 2
        assert ids(await manager.get("/transactions/flagged", params={"reason": "exceeds_daily_limit"})) == [a]
        assert (await manager.get("/transactions/flagged", params={"reason": "nope"})).status_code == 422

    async def test_filter_by_card_driver_truck(self, manager, db, world):
        other = seed.create_world(db)
        mine, theirs = flagged(db, world), flagged(db, other)
        assert ids(await manager.get("/transactions/flagged", params={"card_id": world.card_id})) == [mine]
        assert ids(await manager.get("/transactions/flagged", params={"driver_id": other.driver_id})) == [theirs]
        assert ids(await manager.get("/transactions/flagged", params={"truck_id": other.truck_id})) == [theirs]

    async def test_filter_by_date_uses_whole_utc_days(self, manager, db, world):
        old = flagged(db, world, days_ago=10)
        new = flagged(db, world)
        today = datetime.now(timezone.utc).date()
        ten_days_ago = today - timedelta(days=10)
        assert ids(await manager.get("/transactions/flagged", params={"date_from": str(today)})) == [new]
        assert ids(await manager.get("/transactions/flagged", params={"date_to": str(ten_days_ago)})) == [old]
        both = await manager.get("/transactions/flagged", params={"date_from": str(ten_days_ago), "date_to": str(today)})
        assert ids(both) == [new, old]
        assert (await manager.get("/transactions/flagged", params={"date_from": "bad"})).status_code == 422

    async def test_flagged_is_not_mistaken_for_a_transaction_id(self, manager):
        assert (await manager.get("/transactions/flagged")).status_code == 200
        assert (await manager.get("/transactions/stuck")).status_code == 200
        assert (await manager.get("/transactions/abc")).status_code == 422


class TestReview:
    async def test_review_records_status_note_and_time_and_keeps_the_flags(self, manager, db, world):
        tx = flagged(db, world)
        r = await manager.patch(
            f"/transactions/{tx}/review", json={"review_status": "reviewed", "note": "  Checked with driver "}
        )
        body = r.json()
        assert r.status_code == 200
        assert (body["review_status"], body["review_note"]) == ("reviewed", "Checked with driver")
        assert body["reviewed_at"] is not None and len(body["flags"]) == 1

    async def test_open_and_closed_lists(self, manager, db, world):
        keep, done, dismissed = flagged(db, world), flagged(db, world), flagged(db, world)
        await manager.patch(f"/transactions/{done}/review", json={"review_status": "reviewed"})
        await manager.patch(f"/transactions/{dismissed}/review", json={"review_status": "dismissed"})
        flagged_list = lambda status: manager.get("/transactions/flagged", params={"review_status": status})  # noqa: E731
        assert ids(await flagged_list("none")) == [keep]
        assert ids(await flagged_list("reviewed")) == [done]
        assert ids(await flagged_list("dismissed")) == [dismissed]
        assert len(ids(await manager.get("/transactions/flagged"))) == 3  # nothing is ever removed

    async def test_a_later_review_replaces_the_earlier_one(self, manager, db, world):
        tx = flagged(db, world)
        await manager.patch(f"/transactions/{tx}/review", json={"review_status": "reviewed", "note": "first"})
        body = (await manager.patch(f"/transactions/{tx}/review", json={"review_status": "dismissed"})).json()
        assert (body["review_status"], body["review_note"]) == ("dismissed", None)

    async def test_only_flagged_transactions_can_be_reviewed(self, manager, db, world):
        clean = seed.add_tx(db, world, status=TransactionStatus.APPROVED)
        r = await manager.patch(f"/transactions/{clean}/review", json={"review_status": "reviewed"})
        assert r.status_code == 422 and "flagged" in r.json()["error"]["message"]

    async def test_unknown_transaction_and_invalid_status(self, manager, db, world):
        tx = flagged(db, world)
        assert (await manager.patch("/transactions/999999/review", json={"review_status": "reviewed"})).status_code == 404
        for bad in ("none", "bogus"):
            assert (await manager.patch(f"/transactions/{tx}/review", json={"review_status": bad})).status_code == 422
        assert (await manager.patch(f"/transactions/{tx}/review", json={"review_status": "reviewed", "note": "x" * 1001})).status_code == 422

    async def test_detail_shows_review_fields(self, manager, db, world):
        tx = flagged(db, world)
        await manager.patch(f"/transactions/{tx}/review", json={"review_status": "reviewed", "note": "ok"})
        detail = (await manager.get(f"/transactions/{tx}")).json()
        assert detail["review_status"] == "reviewed" and detail["review_note"] == "ok"


class TestStuck:
    async def test_lists_unfinished_work_only(self, manager, db, world):
        stuck_pending = seed.add_tx(db, world, created_minutes_ago=10)
        stuck_declined = seed.add_declined(db, world, created_minutes_ago=10)
        seed.add_tx(db, world, created_minutes_ago=1)  # too recent
        seed.add_tx(db, world, status=TransactionStatus.APPROVED, created_minutes_ago=60)  # finished
        seed.add_declined(db, world, created_minutes_ago=60, analyzed=True)  # finished
        assert ids(await manager.get("/transactions/stuck")) == [stuck_pending, stuck_declined]

    async def test_older_than_minutes_parameter(self, manager, db, world):
        recent = seed.add_tx(db, world, created_minutes_ago=2)
        assert ids(await manager.get("/transactions/stuck")) == []
        assert ids(await manager.get("/transactions/stuck", params={"older_than_minutes": 1})) == [recent]
        assert ids(await manager.get("/transactions/stuck", params={"older_than_minutes": 0})) == [recent]
        assert (await manager.get("/transactions/stuck", params={"older_than_minutes": -1})).status_code == 422

    async def test_paging(self, manager, db, world):
        a, b = (seed.add_tx(db, world, created_minutes_ago=10) for _ in range(2))
        assert ids(await manager.get("/transactions/stuck", params={"limit": 1, "offset": 1})) == [b]


class TestNotifications:
    async def test_list_newest_first_with_unread_filter(self, manager, db, world):
        tx = flagged(db, world)
        n1, n2 = seed.add_notification(db, tx, "one"), seed.add_notification(db, tx, "two")
        assert ids(await manager.get("/notifications")) == [n2, n1]
        await manager.patch(f"/notifications/{n1}/read")
        assert ids(await manager.get("/notifications", params={"unread": True})) == [n2]
        assert ids(await manager.get("/notifications", params={"unread": False})) == [n1]
        assert ids(await manager.get("/notifications", params={"limit": 1, "offset": 1})) == [n1]

    async def test_mark_read_is_idempotent_and_keeps_the_first_time(self, manager, db, world):
        n = seed.add_notification(db, flagged(db, world))
        first = (await manager.patch(f"/notifications/{n}/read")).json()
        second = (await manager.patch(f"/notifications/{n}/read")).json()
        assert first["read_at"] is not None and first["read_at"] == second["read_at"]

    async def test_unknown_notification(self, manager):
        assert (await manager.patch("/notifications/999999/read")).status_code == 404


class TestReports:
    def add_report(self, db, card_id, day, spend="10"):
        db.add(DailyReport(report_date=day, card_id=card_id, total_gallons=1, total_spend=spend,
                           transaction_count=1, flagged_count=0))  # fmt: skip
        db.commit()

    async def test_list_filters_and_order(self, manager, db, world):
        other = seed.create_world(db)
        d1, d2 = date(2026, 1, 1), date(2026, 1, 2)
        for card, day in ((world.card_id, d1), (world.card_id, d2), (other.card_id, d2)):
            self.add_report(db, card, day)
        rows = (await manager.get("/reports/daily")).json()
        assert [(r["report_date"], r["card_id"]) for r in rows] == [
            ("2026-01-02", world.card_id), ("2026-01-02", other.card_id), ("2026-01-01", world.card_id),
        ]  # fmt: skip
        assert len((await manager.get("/reports/daily", params={"report_date": "2026-01-02"})).json()) == 2
        assert len((await manager.get("/reports/daily", params={"card_id": other.card_id})).json()) == 1
        assert (await manager.get("/reports/daily", params={"report_date": "zzz"})).status_code == 422

    async def test_report_row_shape(self, manager, db, world):
        self.add_report(db, world.card_id, date(2026, 1, 1), spend="12.5")
        row = (await manager.get("/reports/daily")).json()[0]
        assert row == {"report_date": "2026-01-01", "card_id": world.card_id, "total_gallons": "1.000",
                       "total_spend": "12.50", "transaction_count": 1, "flagged_count": 0}  # fmt: skip

    async def test_run_queues_the_job_for_the_given_day(self, manager, queued):
        r = await manager.post("/reports/daily/run", params={"report_date": "2026-03-04"})
        assert r.status_code == 202 and r.json() == {"queued": True, "report_date": "2026-03-04"}
        assert queued == [(names.DAILY_REPORT, ("2026-03-04",))]

    async def test_run_defaults_to_yesterday(self, manager, queued):
        r = await manager.post("/reports/daily/run")
        yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
        assert r.json()["report_date"] == str(yesterday) and queued[0][1] == (str(yesterday),)

    async def test_run_rejects_a_bad_date(self, manager, queued):
        assert (await manager.post("/reports/daily/run", params={"report_date": "zzz"})).status_code == 422
        assert queued == []

    async def test_run_returns_503_when_the_queue_is_down(self, manager, monkeypatch):
        async def down(*a):
            return False

        monkeypatch.setattr("app.api.reports.enqueue", down)
        r = await manager.post("/reports/daily/run")
        assert r.status_code == 503 and r.json()["error"]["message"] == "Job queue is not available"
