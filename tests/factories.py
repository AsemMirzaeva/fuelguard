"""Helpers that build test data through the public API."""

import itertools
from dataclasses import dataclass

_counter = itertools.count(1)


def unique(prefix: str) -> str:
    return f"{prefix}-{next(_counter):05d}"


@dataclass
class ApiWorld:
    station_id: int
    driver_id: int
    truck_id: int
    card_id: int
    card_number: str


async def create_station(manager, name: str | None = None) -> int:
    r = await manager.post("/stations", json={"name": name or unique("Station")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def create_driver(manager, **overrides) -> dict:
    body = {"full_name": "Test Driver", "license_number": unique("LIC")} | overrides
    r = await manager.post("/drivers", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def create_truck(manager, capacity: str = "100", **overrides) -> dict:
    body = {"plate_number": unique("PL"), "tank_capacity_gal": capacity} | overrides
    r = await manager.post("/trucks", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def create_card(manager, driver_id: int, **overrides) -> dict:
    body = {"card_number": unique("CARD"), "driver_id": driver_id} | overrides
    r = await manager.post("/cards", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def assign(manager, driver_id: int, truck_id: int) -> dict:
    r = await manager.post(f"/drivers/{driver_id}/assign-truck", json={"truck_id": truck_id})
    assert r.status_code == 200, r.text
    return r.json()


async def make_world(manager, capacity: str = "100", **card_overrides) -> ApiWorld:
    """Station, active driver assigned to a truck, and an active card."""
    station_id = await create_station(manager)
    driver = await create_driver(manager)
    truck = await create_truck(manager, capacity)
    await assign(manager, driver["id"], truck["id"])
    card = await create_card(manager, driver["id"], **card_overrides)
    return ApiWorld(station_id, driver["id"], truck["id"], card["id"], card["card_number"])


async def purchase(terminal, world_or_card, station_id=None, *, gallons="10", price="30",
                   truck_id=None, key=None):  # fmt: skip
    if isinstance(world_or_card, ApiWorld):
        card_number, station_id = world_or_card.card_number, world_or_card.station_id
    else:
        card_number = world_or_card
    body = {
        "card_number": card_number,
        "station_id": station_id,
        "gallons": gallons,
        "total_price": price,
    }
    if truck_id is not None:
        body["truck_id"] = truck_id
    return await terminal.post(
        "/transactions", json=body, headers={"Idempotency-Key": key or unique("key") + "-xxxx"}
    )
