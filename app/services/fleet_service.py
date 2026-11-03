from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidStateError
from app.models import Driver, DriverTruckAssignment, Truck
from app.schemas.fleet import DriverCreate, DriverUpdate, TruckCreate, TruckUpdate
from app.services import card_service
from app.services.common import commit_or_conflict, get_or_404

LICENSE_TAKEN = "A driver with this license number already exists"
PLATE_TAKEN = "A truck with this plate number already exists"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _current_assignment(db: AsyncSession, driver_id: int) -> DriverTruckAssignment | None:
    stmt = select(DriverTruckAssignment).where(
        DriverTruckAssignment.driver_id == driver_id,
        DriverTruckAssignment.unassigned_at.is_(None),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# ---- drivers -------------------------------------------------------------


async def create_driver(db: AsyncSession, data: DriverCreate) -> Driver:
    driver = Driver(**data.model_dump())
    async with commit_or_conflict(db, LICENSE_TAKEN):
        db.add(driver)
    return driver


async def list_drivers(
    db: AsyncSession, is_active: bool | None, limit: int, offset: int
) -> list[tuple[Driver, int | None]]:
    current = (
        select(DriverTruckAssignment.driver_id, DriverTruckAssignment.truck_id)
        .where(DriverTruckAssignment.unassigned_at.is_(None))
        .subquery()
    )
    stmt = (
        select(Driver, current.c.truck_id)
        .outerjoin(current, current.c.driver_id == Driver.id)
        .order_by(Driver.id)
        .limit(limit)
        .offset(offset)
    )
    if is_active is not None:
        stmt = stmt.where(Driver.is_active == is_active)
    return [(d, truck_id) for d, truck_id in (await db.execute(stmt)).all()]


async def current_truck_id(db: AsyncSession, driver_id: int) -> int | None:
    current = await _current_assignment(db, driver_id)
    return current.truck_id if current else None


async def get_driver(db: AsyncSession, driver_id: int) -> tuple[Driver, int | None]:
    driver = await get_or_404(db, Driver, driver_id, "Driver")
    return driver, await current_truck_id(db, driver_id)


async def update_driver(db: AsyncSession, driver_id: int, data: DriverUpdate) -> Driver:
    driver = await get_or_404(db, Driver, driver_id, "Driver", lock=True)
    async with commit_or_conflict(db, LICENSE_TAKEN):
        for field in data.model_fields_set:
            setattr(driver, field, getattr(data, field))
    return driver


async def deactivate_driver(db: AsyncSession, driver_id: int) -> None:
    """Soft delete: the row stays so old transactions keep their driver."""
    driver = await get_or_404(db, Driver, driver_id, "Driver", lock=True)
    if not driver.is_active:
        return
    driver.is_active = False
    current = await _current_assignment(db, driver_id)
    if current:
        current.unassigned_at = _now()
    await card_service.deactivate_cards_of_driver(db, driver_id)
    await db.commit()


async def reactivate_driver(db: AsyncSession, driver_id: int) -> Driver:
    """Undo a deactivation. Cards and the truck are not restored: a manager sets those again on purpose."""
    driver = await get_or_404(db, Driver, driver_id, "Driver", lock=True)
    if not driver.is_active:
        driver.is_active = True
        await db.commit()
    return driver


async def assign_truck(db: AsyncSession, driver_id: int, truck_id: int) -> DriverTruckAssignment:
    driver = await get_or_404(db, Driver, driver_id, "Driver", lock=True)
    truck = await get_or_404(db, Truck, truck_id, "Truck")
    if not driver.is_active:
        raise InvalidStateError("Driver is inactive")
    if not truck.is_active:
        raise InvalidStateError("Truck is inactive")

    current = await _current_assignment(db, driver_id)
    if current and current.truck_id == truck_id:
        return current  # already assigned: nothing to do

    new = DriverTruckAssignment(driver_id=driver_id, truck_id=truck_id)
    async with commit_or_conflict(db, "Driver was assigned to a truck at the same time"):
        if current:
            current.unassigned_at = _now()
            await db.flush()  # close the old assignment before the unique index sees the new one
        db.add(new)
    return new


async def unassign_truck(db: AsyncSession, driver_id: int) -> DriverTruckAssignment:
    await get_or_404(db, Driver, driver_id, "Driver", lock=True)
    current = await _current_assignment(db, driver_id)
    if current is None:
        raise InvalidStateError("Driver has no truck assigned")
    current.unassigned_at = _now()
    await db.commit()
    return current


async def list_assignments(db: AsyncSession, driver_id: int) -> list[DriverTruckAssignment]:
    await get_or_404(db, Driver, driver_id, "Driver")
    stmt = (
        select(DriverTruckAssignment)
        .where(DriverTruckAssignment.driver_id == driver_id)
        .order_by(DriverTruckAssignment.id)
    )
    return list((await db.execute(stmt)).scalars())


# ---- trucks --------------------------------------------------------------


async def create_truck(db: AsyncSession, data: TruckCreate) -> Truck:
    truck = Truck(**data.model_dump())
    async with commit_or_conflict(db, PLATE_TAKEN):
        db.add(truck)
    await db.refresh(truck)
    return truck


async def list_trucks(
    db: AsyncSession, is_active: bool | None, limit: int, offset: int
) -> list[Truck]:
    stmt = select(Truck).order_by(Truck.id).limit(limit).offset(offset)
    if is_active is not None:
        stmt = stmt.where(Truck.is_active == is_active)
    return list((await db.execute(stmt)).scalars())


async def get_truck(db: AsyncSession, truck_id: int) -> Truck:
    return await get_or_404(db, Truck, truck_id, "Truck")


async def update_truck(db: AsyncSession, truck_id: int, data: TruckUpdate) -> Truck:
    truck = await get_or_404(db, Truck, truck_id, "Truck", lock=True)
    async with commit_or_conflict(db, PLATE_TAKEN):
        for field in data.model_fields_set:
            setattr(truck, field, getattr(data, field))
    await db.refresh(truck)
    return truck


async def deactivate_truck(db: AsyncSession, truck_id: int) -> None:
    truck = await get_or_404(db, Truck, truck_id, "Truck", lock=True)
    if not truck.is_active:
        return
    truck.is_active = False
    stmt = select(DriverTruckAssignment).where(
        DriverTruckAssignment.truck_id == truck_id,
        DriverTruckAssignment.unassigned_at.is_(None),
    )
    for assignment in (await db.execute(stmt)).scalars():
        assignment.unassigned_at = _now()
    await db.commit()


async def reactivate_truck(db: AsyncSession, truck_id: int) -> Truck:
    """Undo a deactivation. Past driver assignments stay closed."""
    truck = await get_or_404(db, Truck, truck_id, "Truck", lock=True)
    if not truck.is_active:
        truck.is_active = True
        await db.commit()
    await db.refresh(truck)
    return truck
