import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models import FuelTransaction
from app.workers import names
from tests.factories import assign, create_card, create_driver, create_truck, make_world, purchase, unique


def count_transactions(db) -> int:
    return db.scalar(select(func.count()).select_from(FuelTransaction))


class TestAccepted:
    async def test_pending_authorized_with_server_side_fields(self, manager, terminal):
        w = await make_world(manager)
        before = datetime.now(timezone.utc)
        r = await purchase(terminal, w, gallons="42.5", price="161.50")
        body = r.json()
        assert r.status_code == 201
        assert body["status"] == "pending" and body["authorized"] is True
        assert body["decline_reason"] is None and body["review_status"] == "none"
        assert (body["card_id"], body["driver_id"], body["truck_id"], body["station_id"]) == (
            w.card_id, w.driver_id, w.truck_id, w.station_id,
        )  # fmt: skip
        assert body["gallons"] == "42.500" and body["total_price"] == "161.50"
        stamped = datetime.fromisoformat(body["transaction_time"])
        assert before - timedelta(seconds=1) <= stamped <= datetime.now(timezone.utc) + timedelta(seconds=1)

    async def test_client_cannot_choose_the_transaction_time(self, manager, terminal):
        w = await make_world(manager)
        r = await terminal.post(
            "/transactions",
            headers={"Idempotency-Key": unique("k") + "-xxxx"},
            json={"card_number": w.card_number, "station_id": w.station_id, "gallons": "5",
                  "total_price": "10", "transaction_time": "2001-01-01T00:00:00Z"},
        )  # fmt: skip
        assert r.status_code == 201 and r.json()["transaction_time"].startswith(str(datetime.now().year))

    async def test_matching_truck_id_is_accepted(self, manager, terminal):
        w = await make_world(manager)
        r = await purchase(terminal, w, truck_id=w.truck_id)
        assert r.status_code == 201 and r.json()["status"] == "pending"

    async def test_analysis_job_is_queued_after_the_save(self, manager, terminal, queued):
        w = await make_world(manager)
        tx_id = (await purchase(terminal, w)).json()["id"]
        assert queued == [(names.ANALYZE_TRANSACTION, (tx_id,))]

    async def test_limits_do_not_decline_at_the_pump(self, manager, terminal):
        w = await make_world(manager, per_transaction_limit="1", daily_limit="1")
        r = await purchase(terminal, w, gallons="500", price="9999")
        assert r.status_code == 201 and r.json()["authorized"] is True  # flagged later, not declined


class TestDeclined:
    async def test_inactive_card(self, manager, terminal, queued):
        w = await make_world(manager)
        await manager.patch(f"/cards/{w.card_id}/status", json={"status": "inactive", "comment": "test"})
        r = await purchase(terminal, w)
        body = r.json()
        assert r.status_code == 201
        assert (body["status"], body["authorized"], body["decline_reason"]) == ("declined", False, "card_inactive")
        assert body["truck_id"] == w.truck_id
        assert queued == [(names.CHECK_DECLINED_ATTEMPT, (body["id"],))]

    async def test_blocked_card(self, manager, terminal):
        w = await make_world(manager)
        await manager.patch(f"/cards/{w.card_id}/status", json={"status": "blocked", "comment": "test"})
        assert (await purchase(terminal, w)).json()["decline_reason"] == "card_blocked"

    async def test_card_works_again_after_the_block_is_lifted(self, manager, terminal):
        w = await make_world(manager)
        await manager.patch(f"/cards/{w.card_id}/status", json={"status": "blocked", "comment": "test"})
        assert (await purchase(terminal, w)).json()["authorized"] is False
        await manager.patch(f"/cards/{w.card_id}/status", json={"status": "active", "comment": "Found"})
        assert (await purchase(terminal, w)).json()["authorized"] is True

    async def test_driver_without_truck(self, manager, terminal):
        d = await create_driver(manager)
        card = await create_card(manager, d["id"])
        w = await make_world(manager)
        r = await purchase(terminal, card["card_number"], w.station_id)
        assert r.json()["decline_reason"] == "no_vehicle" and r.json()["truck_id"] is None

    async def test_truck_id_without_an_assignment_is_still_no_vehicle(self, manager, terminal):
        d, t = await create_driver(manager), await create_truck(manager)
        card = await create_card(manager, d["id"])
        w = await make_world(manager)
        r = await purchase(terminal, card["card_number"], w.station_id, truck_id=t["id"])
        assert r.json()["decline_reason"] == "no_vehicle"

    async def test_other_existing_truck_is_a_mismatch_and_records_that_truck(self, manager, terminal):
        w = await make_world(manager)
        other = await create_truck(manager)
        body = (await purchase(terminal, w, truck_id=other["id"])).json()
        assert (body["decline_reason"], body["truck_id"]) == ("vehicle_mismatch", other["id"])

    async def test_unknown_truck_is_a_mismatch_with_no_truck_recorded(self, manager, terminal):
        w = await make_world(manager)
        body = (await purchase(terminal, w, truck_id=999999)).json()
        assert (body["decline_reason"], body["truck_id"]) == ("vehicle_mismatch", None)

    async def test_manager_can_allow_another_truck_by_reassigning(self, manager, terminal):
        w = await make_world(manager)
        other = await create_truck(manager)
        assert (await purchase(terminal, w, truck_id=other["id"])).json()["authorized"] is False
        await assign(manager, w.driver_id, other["id"])
        r = await purchase(terminal, w, truck_id=other["id"])
        assert r.json()["authorized"] is True and r.json()["truck_id"] == other["id"]

    @pytest.mark.parametrize(
        "card_status, has_truck, wrong_truck, expected",
        [
            ("blocked", False, False, "card_blocked"),
            ("blocked", True, True, "card_blocked"),
            ("inactive", False, False, "card_inactive"),
            ("inactive", True, True, "card_inactive"),
            ("active", False, False, "no_vehicle"),
            ("active", False, True, "no_vehicle"),
            ("active", True, True, "vehicle_mismatch"),
        ],
    )
    async def test_reasons_are_checked_in_a_fixed_order(
        self, manager, terminal, card_status, has_truck, wrong_truck, expected
    ):
        station = (await make_world(manager)).station_id
        d = await create_driver(manager)
        truck = await create_truck(manager)
        if has_truck:
            await assign(manager, d["id"], truck["id"])
        card = await create_card(manager, d["id"])
        if card_status != "active":
            await manager.patch(f"/cards/{card['id']}/status", json={"status": card_status, "comment": "test"})
        other = await create_truck(manager)
        r = await purchase(terminal, card["card_number"], station, truck_id=other["id"] if wrong_truck else None)
        assert r.json()["decline_reason"] == expected

    async def test_declined_attempts_are_stored_not_discarded(self, manager, terminal, db):
        w = await make_world(manager)
        await manager.patch(f"/cards/{w.card_id}/status", json={"status": "blocked", "comment": "test"})
        for _ in range(3):
            await purchase(terminal, w)
        assert count_transactions(db) == 3


class TestRejected:
    async def test_unknown_card_creates_nothing(self, manager, terminal, db, queued):
        w = await make_world(manager)
        r = await purchase(terminal, "NO-SUCH-CARD", w.station_id)
        assert r.status_code == 404 and "not found" in r.json()["error"]["message"]
        assert count_transactions(db) == 0 and queued == []

    async def test_unknown_station(self, manager, terminal, db):
        w = await make_world(manager)
        r = await purchase(terminal, w.card_number, 999999)
        assert r.status_code == 404 and count_transactions(db) == 0

    @pytest.mark.parametrize(
        "gallons, price",
        [("0", "10"), ("-5", "10"), ("5", "-1"), ("abc", "10"), ("1.2345", "10"), ("5", "10.001")],
    )
    async def test_bad_numbers(self, manager, terminal, gallons, price):
        w = await make_world(manager)
        assert (await purchase(terminal, w, gallons=gallons, price=price)).status_code == 422

    @pytest.mark.parametrize("key", [None, "short"])
    async def test_idempotency_key_is_required_and_long_enough(self, manager, terminal, key):
        w = await make_world(manager)
        headers = {"Idempotency-Key": key} if key else {}
        body = {"card_number": w.card_number, "station_id": w.station_id, "gallons": "1", "total_price": "1"}
        assert (await terminal.post("/transactions", json=body, headers=headers)).status_code == 422

    async def test_missing_fields(self, terminal):
        r = await terminal.post("/transactions", json={}, headers={"Idempotency-Key": "k" * 10})
        assert r.status_code == 422


class TestIdempotency:
    async def test_same_key_and_body_returns_the_first_result_without_new_work(
        self, manager, terminal, db, queued
    ):
        w = await make_world(manager)
        first = await purchase(terminal, w, key="same-key-0001")
        again = await purchase(terminal, w, key="same-key-0001")
        assert (first.status_code, again.status_code) == (201, 200)
        assert first.json() == again.json()
        assert count_transactions(db) == 1 and len(queued) == 1

    async def test_replay_of_a_declined_attempt(self, manager, terminal, queued):
        w = await make_world(manager)
        await manager.patch(f"/cards/{w.card_id}/status", json={"status": "blocked", "comment": "test"})
        a = await purchase(terminal, w, key="declined-key-1")
        b = await purchase(terminal, w, key="declined-key-1")
        assert b.status_code == 200 and a.json()["id"] == b.json()["id"] and len(queued) == 1

    async def test_replay_returns_the_stored_decision_even_if_the_card_changed(self, manager, terminal):
        w = await make_world(manager)
        first = await purchase(terminal, w, key="stable-key-01")
        await manager.patch(f"/cards/{w.card_id}/status", json={"status": "blocked", "comment": "test"})
        again = await purchase(terminal, w, key="stable-key-01")
        assert again.status_code == 200
        assert first.json()["authorized"] is True and again.json()["authorized"] is True

    @pytest.mark.parametrize("change", [{"gallons": "11"}, {"price": "31"}])
    async def test_same_key_with_a_different_body_is_a_conflict(self, manager, terminal, change):
        w = await make_world(manager)
        await purchase(terminal, w, key="reused-key-01")
        r = await purchase(terminal, w, key="reused-key-01", **change)
        assert r.status_code == 409 and "different request" in r.json()["error"]["message"]

    async def test_same_key_for_another_card_is_a_conflict(self, manager, terminal):
        a, b = await make_world(manager), await make_world(manager)
        await purchase(terminal, a, key="shared-key-01")
        assert (await purchase(terminal, b, key="shared-key-01")).status_code == 409

    async def test_equivalent_numbers_are_the_same_request(self, manager, terminal):
        w = await make_world(manager)
        await purchase(terminal, w, key="numeric-key-1", gallons="10", price="30")
        assert (await purchase(terminal, w, key="numeric-key-1", gallons="10.000", price="30.00")).status_code == 200

    async def test_parallel_retries_create_exactly_one_transaction(self, manager, terminal, db, queued):
        w = await make_world(manager)
        # Several rounds: the unique-constraint race only happens when requests overlap.
        for round_no in range(5):
            key = f"race-key-{round_no:04d}"
            results = await asyncio.gather(*[purchase(terminal, w, key=key) for _ in range(10)])
            assert sorted(r.status_code for r in results) == [200] * 9 + [201]
            assert len({r.json()["id"] for r in results}) == 1
        assert count_transactions(db) == 5 and len(queued) == 5


class TestUnexpectedDatabaseErrors:
    async def test_a_constraint_error_that_is_not_a_duplicate_key_is_not_swallowed(
        self, manager, terminal_no_raise, monkeypatch, db
    ):
        """If the database rejects the insert and no earlier transaction explains it, fail loudly."""
        from app.services import transaction_service

        async def never_found(db, key):
            return None

        w = await make_world(manager)
        await purchase(terminal_no_raise, w, key="dup-key-00001")
        monkeypatch.setattr(transaction_service, "_find_by_key", never_found)
        r = await purchase(terminal_no_raise, w, key="dup-key-00001")  # unique violation, nothing to replay
        assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"
        assert count_transactions(db) == 1


class TestQueueOutage:
    async def test_purchase_still_succeeds_when_the_broker_is_down(self, manager, terminal, monkeypatch, caplog):
        from app.workers import dispatch

        def down(*a, **kw):
            raise ConnectionError("redis is down")

        monkeypatch.setattr(dispatch.celery_app, "send_task", down)
        monkeypatch.setattr("app.api.transactions.enqueue", dispatch.enqueue)  # the real one
        w = await make_world(manager)
        r = await purchase(terminal, w)
        assert r.status_code == 201 and r.json()["status"] == "pending"
        assert "recovery job will retry" in caplog.text
        stuck = (await manager.get("/transactions/stuck", params={"older_than_minutes": 0})).json()
        assert [t["id"] for t in stuck] == [r.json()["id"]]


class TestSnapshot:
    async def test_transaction_keeps_the_truck_used_at_the_time(self, manager, terminal):
        w = await make_world(manager)
        tx = (await purchase(terminal, w)).json()
        newer = await create_truck(manager)
        await assign(manager, w.driver_id, newer["id"])
        stored = (await manager.get(f"/transactions/{tx['id']}")).json()
        assert stored["truck_id"] == w.truck_id


class TestReadingTransactions:
    async def test_list_filters_newest_first(self, manager, terminal):
        a, b = await make_world(manager), await make_world(manager)
        t1 = (await purchase(terminal, a)).json()["id"]
        t2 = (await purchase(terminal, b)).json()["id"]
        await manager.patch(f"/cards/{b.card_id}/status", json={"status": "blocked", "comment": "test"})
        t3 = (await purchase(terminal, b)).json()["id"]

        def ids(r):
            return [t["id"] for t in r.json()]

        assert ids(await manager.get("/transactions")) == [t3, t2, t1]
        assert ids(await manager.get("/transactions", params={"card_id": a.card_id})) == [t1]
        assert ids(await manager.get("/transactions", params={"driver_id": b.driver_id})) == [t3, t2]
        assert ids(await manager.get("/transactions", params={"truck_id": b.truck_id})) == [t3, t2]
        assert ids(await manager.get("/transactions", params={"status": "declined"})) == [t3]
        assert ids(await manager.get("/transactions", params={"limit": 1, "offset": 1})) == [t2]

    async def test_detail_includes_flags_list_and_404(self, manager, terminal):
        w = await make_world(manager)
        tx = (await purchase(terminal, w)).json()
        detail = (await manager.get(f"/transactions/{tx['id']}")).json()
        assert detail["flags"] == [] and detail["analysis_done_at"] is None
        assert (await manager.get("/transactions/999999")).status_code == 404
