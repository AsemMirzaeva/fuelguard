import asyncio

import pytest

from tests.factories import assign, create_card, create_driver, create_truck, unique


class TestDrivers:
    async def test_create_trims_text_and_defaults(self, manager):
        r = await manager.post(
            "/drivers", json={"full_name": "  John Smith ", "license_number": " L-100 "}
        )
        body = r.json()
        assert r.status_code == 201
        assert body["full_name"] == "John Smith" and body["license_number"] == "L-100"
        assert body["is_active"] is True and body["current_truck_id"] is None and body["phone"] is None

    async def test_duplicate_license_is_a_conflict(self, manager):
        d = await create_driver(manager)
        r = await manager.post("/drivers", json={"full_name": "B", "license_number": d["license_number"]})
        assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"

    @pytest.mark.parametrize("body", [{}, {"full_name": "A"}, {"full_name": "A", "license_number": "x"}])
    async def test_create_validation(self, manager, body):
        assert (await manager.post("/drivers", json=body)).status_code == 422

    async def test_get_and_404(self, manager):
        d = await create_driver(manager)
        assert (await manager.get(f"/drivers/{d['id']}")).json()["id"] == d["id"]
        r = await manager.get("/drivers/999999")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"

    async def test_list_pagination_and_filter(self, manager):
        ids = [(await create_driver(manager))["id"] for _ in range(5)]
        await manager.delete(f"/drivers/{ids[0]}")
        page = (await manager.get("/drivers", params={"limit": 2, "offset": 1})).json()
        assert [d["id"] for d in page] == ids[1:3]
        active = (await manager.get("/drivers", params={"is_active": True})).json()
        inactive = (await manager.get("/drivers", params={"is_active": False})).json()
        assert [d["id"] for d in inactive] == [ids[0]] and ids[0] not in [d["id"] for d in active]

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
    async def test_list_rejects_bad_paging(self, manager, params):
        assert (await manager.get("/drivers", params=params)).status_code == 422

    async def test_patch_changes_only_sent_fields_and_can_clear_phone(self, manager):
        d = await create_driver(manager, phone="+1 555 0100")
        r = await manager.patch(f"/drivers/{d['id']}", json={"full_name": "New Name"})
        assert r.json()["full_name"] == "New Name" and r.json()["phone"] == "+1 555 0100"
        r = await manager.patch(f"/drivers/{d['id']}", json={"phone": None})
        assert r.json()["phone"] is None and r.json()["full_name"] == "New Name"

    async def test_patch_cannot_null_required_fields(self, manager):
        d = await create_driver(manager)
        assert (await manager.patch(f"/drivers/{d['id']}", json={"full_name": None})).status_code == 422

    async def test_patch_duplicate_license_is_a_conflict(self, manager):
        a, b = await create_driver(manager), await create_driver(manager)
        r = await manager.patch(f"/drivers/{b['id']}", json={"license_number": a["license_number"]})
        assert r.status_code == 409

    async def test_patch_unknown_driver(self, manager):
        assert (await manager.patch("/drivers/999999", json={"phone": "1"})).status_code == 404


class TestDriverSoftDelete:
    async def test_row_is_kept_and_delete_is_idempotent(self, manager):
        d = await create_driver(manager)
        assert (await manager.delete(f"/drivers/{d['id']}")).status_code == 204
        assert (await manager.delete(f"/drivers/{d['id']}")).status_code == 204
        assert (await manager.get(f"/drivers/{d['id']}")).json()["is_active"] is False

    async def test_unknown_driver(self, manager):
        assert (await manager.delete("/drivers/999999")).status_code == 404

    async def test_closes_assignment_and_switches_off_active_cards_only(self, manager):
        d, t = await create_driver(manager), await create_truck(manager)
        await assign(manager, d["id"], t["id"])
        active = await create_card(manager, d["id"])
        blocked = await create_card(manager, d["id"])
        await manager.patch(f"/cards/{blocked['id']}/status", json={"status": "blocked"})

        await manager.delete(f"/drivers/{d['id']}")

        assert (await manager.get(f"/drivers/{d['id']}")).json()["current_truck_id"] is None
        assert (await manager.get(f"/cards/{active['id']}")).json()["status"] == "inactive"
        assert (await manager.get(f"/cards/{blocked['id']}")).json()["status"] == "blocked"
        history = (await manager.get(f"/cards/{active['id']}/history")).json()
        assert history[-1]["changed_by"] == "system:driver_deactivated"
        assert history[-1]["old_status"] == "active" and history[-1]["new_status"] == "inactive"

    async def test_inactive_driver_cannot_get_a_card_or_truck(self, manager):
        d, t = await create_driver(manager), await create_truck(manager)
        await manager.delete(f"/drivers/{d['id']}")
        r = await manager.post("/cards", json={"card_number": unique("C"), "driver_id": d["id"]})
        assert r.status_code == 422 and "inactive driver" in r.json()["error"]["message"]
        r = await manager.post(f"/drivers/{d['id']}/assign-truck", json={"truck_id": t["id"]})
        assert r.status_code == 422


class TestTrucks:
    async def test_create_normalizes_capacity(self, manager):
        r = await manager.post("/trucks", json={"plate_number": " TX-1 ", "tank_capacity_gal": "120"})
        assert r.status_code == 201
        assert r.json()["tank_capacity_gal"] == "120.00" and r.json()["plate_number"] == "TX-1"

    @pytest.mark.parametrize("capacity", ["0", "-5", "abc", "1234567.89"])
    async def test_capacity_validation(self, manager, capacity):
        r = await manager.post("/trucks", json={"plate_number": unique("P"), "tank_capacity_gal": capacity})
        assert r.status_code == 422

    async def test_duplicate_plate(self, manager):
        t = await create_truck(manager)
        r = await manager.post("/trucks", json={"plate_number": t["plate_number"], "tank_capacity_gal": "10"})
        assert r.status_code == 409

    async def test_get_list_patch(self, manager):
        t = await create_truck(manager, "100")
        assert (await manager.get(f"/trucks/{t['id']}")).status_code == 200
        r = await manager.patch(f"/trucks/{t['id']}", json={"tank_capacity_gal": "250.5", "model": "Volvo"})
        assert r.json()["tank_capacity_gal"] == "250.50" and r.json()["model"] == "Volvo"
        assert [x["id"] for x in (await manager.get("/trucks")).json()] == [t["id"]]
        assert (await manager.get("/trucks", params={"is_active": False})).json() == []
        assert [x["id"] for x in (await manager.get("/trucks", params={"is_active": True})).json()] == [t["id"]]
        assert (await manager.patch(f"/trucks/{t['id']}", json={"plate_number": None})).status_code == 422
        assert (await manager.get("/trucks/999999")).status_code == 404

    async def test_patch_duplicate_plate(self, manager):
        a, b = await create_truck(manager), await create_truck(manager)
        r = await manager.patch(f"/trucks/{b['id']}", json={"plate_number": a["plate_number"]})
        assert r.status_code == 409

    async def test_soft_delete_closes_assignments_and_blocks_new_ones(self, manager):
        t, d = await create_truck(manager), await create_driver(manager)
        await assign(manager, d["id"], t["id"])
        assert (await manager.delete(f"/trucks/{t['id']}")).status_code == 204
        assert (await manager.delete(f"/trucks/{t['id']}")).status_code == 204
        assert (await manager.get(f"/trucks/{t['id']}")).json()["is_active"] is False
        assert (await manager.get(f"/drivers/{d['id']}")).json()["current_truck_id"] is None
        r = await manager.post(f"/drivers/{d['id']}/assign-truck", json={"truck_id": t["id"]})
        assert r.status_code == 422 and "Truck is inactive" in r.json()["error"]["message"]
        assert (await manager.delete("/trucks/999999")).status_code == 404


class TestAssignments:
    async def test_assign_shows_current_truck_everywhere(self, manager):
        d, t = await create_driver(manager), await create_truck(manager)
        a = await assign(manager, d["id"], t["id"])
        assert a["unassigned_at"] is None and a["truck_id"] == t["id"]
        assert (await manager.get(f"/drivers/{d['id']}")).json()["current_truck_id"] == t["id"]
        listed = {x["id"]: x for x in (await manager.get("/drivers")).json()}
        assert listed[d["id"]]["current_truck_id"] == t["id"]

    async def test_assigning_the_same_truck_again_changes_nothing(self, manager):
        d, t = await create_driver(manager), await create_truck(manager)
        first, second = await assign(manager, d["id"], t["id"]), await assign(manager, d["id"], t["id"])
        assert first["id"] == second["id"]
        assert len((await manager.get(f"/drivers/{d['id']}/assignments")).json()) == 1

    async def test_reassigning_closes_the_old_assignment_and_keeps_history(self, manager):
        d, t1, t2 = await create_driver(manager), await create_truck(manager), await create_truck(manager)
        await assign(manager, d["id"], t1["id"])
        await assign(manager, d["id"], t2["id"])
        history = (await manager.get(f"/drivers/{d['id']}/assignments")).json()
        assert [h["truck_id"] for h in history] == [t1["id"], t2["id"]]
        assert history[0]["unassigned_at"] is not None and history[1]["unassigned_at"] is None
        assert (await manager.get(f"/drivers/{d['id']}")).json()["current_truck_id"] == t2["id"]

    async def test_two_drivers_can_share_a_truck(self, manager):
        t = await create_truck(manager)
        a, b = await create_driver(manager), await create_driver(manager)
        await assign(manager, a["id"], t["id"])
        await assign(manager, b["id"], t["id"])
        assert (await manager.get(f"/drivers/{a['id']}")).json()["current_truck_id"] == t["id"]
        assert (await manager.get(f"/drivers/{b['id']}")).json()["current_truck_id"] == t["id"]

    async def test_unassign(self, manager):
        d, t = await create_driver(manager), await create_truck(manager)
        await assign(manager, d["id"], t["id"])
        r = await manager.post(f"/drivers/{d['id']}/unassign-truck")
        assert r.status_code == 200 and r.json()["unassigned_at"] is not None
        assert (await manager.get(f"/drivers/{d['id']}")).json()["current_truck_id"] is None
        again = await manager.post(f"/drivers/{d['id']}/unassign-truck")
        assert again.status_code == 422 and "no truck" in again.json()["error"]["message"]

    async def test_unknown_driver_or_truck(self, manager):
        d, t = await create_driver(manager), await create_truck(manager)
        assert (await manager.post(f"/drivers/{d['id']}/assign-truck", json={"truck_id": 999999})).status_code == 404
        assert (await manager.post(f"/drivers/999999/assign-truck", json={"truck_id": t["id"]})).status_code == 404
        assert (await manager.post("/drivers/999999/unassign-truck")).status_code == 404
        assert (await manager.get("/drivers/999999/assignments")).status_code == 404

    async def test_parallel_assignments_leave_exactly_one_current_truck(self, manager):
        d = await create_driver(manager)
        trucks = [await create_truck(manager) for _ in range(4)]
        results = await asyncio.gather(
            *[manager.post(f"/drivers/{d['id']}/assign-truck", json={"truck_id": t["id"]}) for t in trucks]
        )
        assert all(r.status_code in (200, 409) for r in results)
        history = (await manager.get(f"/drivers/{d['id']}/assignments")).json()
        assert sum(1 for h in history if h["unassigned_at"] is None) == 1
