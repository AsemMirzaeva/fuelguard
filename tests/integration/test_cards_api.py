import asyncio

import pytest

from tests.factories import create_card, create_driver, unique
from tests.tokens import MANAGER_USERNAME


class TestCreate:
    async def test_new_card_is_active_with_a_history_entry(self, manager):
        d = await create_driver(manager)
        card = await create_card(manager, d["id"])
        assert card["status"] == "active" and card["driver_id"] == d["id"]
        history = (await manager.get(f"/cards/{card['id']}/history")).json()
        assert [(h["old_status"], h["new_status"]) for h in history] == [(None, "active")]

    async def test_limits_are_optional_and_normalized(self, manager):
        d = await create_driver(manager)
        bare = await create_card(manager, d["id"])
        assert [bare[k] for k in ("per_transaction_limit", "daily_limit", "weekly_limit", "monthly_limit")] == [None] * 4
        full = await create_card(manager, d["id"], per_transaction_limit="300", daily_limit="500.5",
                                 weekly_limit="2500", monthly_limit="8000")  # fmt: skip
        assert (full["per_transaction_limit"], full["daily_limit"]) == ("300.00", "500.50")

    async def test_duplicate_number(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        r = await manager.post("/cards", json={"card_number": c["card_number"], "driver_id": d["id"]})
        assert r.status_code == 409

    async def test_unknown_driver(self, manager):
        r = await manager.post("/cards", json={"card_number": unique("C"), "driver_id": 999999})
        assert r.status_code == 404

    @pytest.mark.parametrize("limit", ["0", "-1", "abc", "1.234"])
    async def test_bad_limits_rejected(self, manager, limit):
        d = await create_driver(manager)
        r = await manager.post(
            "/cards", json={"card_number": unique("C"), "driver_id": d["id"], "daily_limit": limit}
        )
        assert r.status_code == 422

    async def test_parallel_duplicate_numbers_create_only_one_card(self, manager):
        d = await create_driver(manager)
        body = {"card_number": "RACE-1234", "driver_id": d["id"]}
        results = await asyncio.gather(*[manager.post("/cards", json=body) for _ in range(5)])
        assert sorted(r.status_code for r in results) == [201, 409, 409, 409, 409]


class TestLimits:
    async def test_change_one_limit_keeps_the_others(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"], per_transaction_limit="300", daily_limit="500")
        r = await manager.patch(f"/cards/{c['id']}/limits", json={"daily_limit": "650.50"})
        body = r.json()
        assert (body["per_transaction_limit"], body["daily_limit"]) == ("300.00", "650.50")

    async def test_explicit_null_removes_a_limit(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"], weekly_limit="100", monthly_limit="400")
        body = (await manager.patch(f"/cards/{c['id']}/limits", json={"weekly_limit": None})).json()
        assert body["weekly_limit"] is None and body["monthly_limit"] == "400.00"

    async def test_empty_body_changes_nothing(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"], daily_limit="100")
        body = (await manager.patch(f"/cards/{c['id']}/limits", json={})).json()
        assert body["daily_limit"] == "100.00"

    async def test_invalid_and_unknown(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        assert (await manager.patch(f"/cards/{c['id']}/limits", json={"daily_limit": "0"})).status_code == 422
        assert (await manager.patch("/cards/999999/limits", json={})).status_code == 404


class TestStatus:
    @pytest.mark.parametrize(
        "path",
        [["inactive"], ["blocked"], ["inactive", "active"], ["blocked", "active"],
         ["inactive", "blocked", "inactive", "active"]],
    )  # fmt: skip
    async def test_every_transition_is_allowed_including_lifting_a_block(self, manager, path):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        for status in path:
            r = await manager.patch(f"/cards/{c['id']}/status", json={"status": status, "comment": "why"})
            assert r.status_code == 200 and r.json()["status"] == status
        history = (await manager.get(f"/cards/{c['id']}/history")).json()
        assert [h["new_status"] for h in history] == ["active", *path]
        assert all(h["changed_by"] == MANAGER_USERNAME for h in history)

    async def test_deactivating_or_blocking_needs_a_comment(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        for status in ("inactive", "blocked"):
            for body in ({"status": status}, {"status": status, "comment": ""}, {"status": status, "comment": "   "},
                         {"status": status, "comment": None}):
                r = await manager.patch(f"/cards/{c['id']}/status", json=body)
                assert r.status_code == 422, body
                assert f"A comment is required to make a card {status}" in str(r.json())
        assert (await manager.get(f"/cards/{c['id']}")).json()["status"] == "active"
        assert len((await manager.get(f"/cards/{c['id']}/history")).json()) == 1

    async def test_the_comment_is_trimmed_and_kept_in_the_history(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        await manager.patch(f"/cards/{c['id']}/status", json={"status": "blocked", "comment": "  Lost by driver  "})
        await manager.patch(f"/cards/{c['id']}/status", json={"status": "active", "comment": "Found it"})
        await manager.patch(f"/cards/{c['id']}/status", json={"status": "inactive", "comment": "x" * 500})
        history = (await manager.get(f"/cards/{c['id']}/history")).json()
        assert [h["comment"] for h in history] == [None, "Lost by driver", "Found it", "x" * 500]

    async def test_activating_needs_no_comment_and_a_blank_one_is_stored_as_none(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        await manager.patch(f"/cards/{c['id']}/status", json={"status": "inactive", "comment": "pause"})
        r = await manager.patch(f"/cards/{c['id']}/status", json={"status": "active", "comment": "  "})
        assert r.status_code == 200
        assert (await manager.get(f"/cards/{c['id']}/history")).json()[-1]["comment"] is None

    async def test_a_comment_over_500_characters_is_refused(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        r = await manager.patch(f"/cards/{c['id']}/status", json={"status": "blocked", "comment": "x" * 501})
        assert r.status_code == 422

    async def test_same_status_is_a_conflict_and_not_logged(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        r = await manager.patch(f"/cards/{c['id']}/status", json={"status": "active"})
        assert r.status_code == 409 and "already active" in r.json()["error"]["message"]
        assert len((await manager.get(f"/cards/{c['id']}/history")).json()) == 1

    async def test_unknown_status_and_unknown_card(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        assert (await manager.patch(f"/cards/{c['id']}/status", json={"status": "bogus"})).status_code == 422
        assert (await manager.patch("/cards/999999/status", json={"status": "blocked", "comment": "test"})).status_code == 404
        assert (await manager.get("/cards/999999/history")).status_code == 404

    async def test_cannot_activate_a_card_of_an_inactive_driver(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        await manager.delete(f"/drivers/{d['id']}")  # card becomes inactive
        r = await manager.patch(f"/cards/{c['id']}/status", json={"status": "active"})
        assert r.status_code == 422 and "inactive driver" in r.json()["error"]["message"]

    async def test_opposite_changes_in_parallel_leave_a_consistent_history(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        results = await asyncio.gather(
            *[manager.patch(f"/cards/{c['id']}/status", json={"status": "blocked", "comment": "test"}) for _ in range(5)]
        )
        assert sorted(r.status_code for r in results) == [200, 409, 409, 409, 409]
        assert len((await manager.get(f"/cards/{c['id']}/history")).json()) == 2


class TestListAndGet:
    async def test_filters(self, manager):
        a, b = await create_driver(manager), await create_driver(manager)
        c1, c2 = await create_card(manager, a["id"]), await create_card(manager, b["id"])
        await manager.patch(f"/cards/{c2['id']}/status", json={"status": "blocked", "comment": "test"})
        ids = lambda r: [c["id"] for c in r.json()]  # noqa: E731
        assert ids(await manager.get("/cards", params={"driver_id": a["id"]})) == [c1["id"]]
        assert ids(await manager.get("/cards", params={"status": "blocked"})) == [c2["id"]]
        assert ids(await manager.get("/cards", params={"driver_id": a["id"], "status": "blocked"})) == []
        assert ids(await manager.get("/cards", params={"limit": 1, "offset": 1})) == [c2["id"]]
        assert (await manager.get("/cards", params={"status": "nope"})).status_code == 422

    async def test_get(self, manager):
        d = await create_driver(manager)
        c = await create_card(manager, d["id"])
        assert (await manager.get(f"/cards/{c['id']}")).json()["card_number"] == c["card_number"]
        assert (await manager.get("/cards/999999")).status_code == 404


class TestStations:
    async def test_create_list_get(self, manager):
        r = await manager.post("/stations", json={"name": " Shell 10 ", "address": "Exit 42"})
        assert r.status_code == 201 and r.json()["name"] == "Shell 10"
        sid = r.json()["id"]
        assert (await manager.get(f"/stations/{sid}")).json()["address"] == "Exit 42"
        assert [s["id"] for s in (await manager.get("/stations")).json()] == [sid]
        assert (await manager.get("/stations/999999")).status_code == 404
        assert (await manager.post("/stations", json={"name": ""})).status_code == 422
