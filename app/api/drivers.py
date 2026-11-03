from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageParams
from app.core.errors import AUTH_ERRORS, CONFLICT, INVALID_STATE, NOT_FOUND
from app.core.security import require_manager, require_reader
from app.db.async_db import get_db
from app.models import Driver
from app.schemas.fleet import (
    AssignmentRead,
    AssignTruckRequest,
    DriverCreate,
    DriverRead,
    DriverUpdate,
)
from app.services import fleet_service

router = APIRouter(
    prefix="/drivers",
    tags=["Drivers"],
    dependencies=[Depends(require_reader)],
    responses=AUTH_ERRORS,
)


def _out(driver: Driver, current_truck_id: int | None = None) -> DriverRead:
    return DriverRead.model_validate(driver).model_copy(
        update={"current_truck_id": current_truck_id}
    )


@router.post(
    "",
    response_model=DriverRead,
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT,
    dependencies=[Depends(require_manager)],
)
async def create_driver(data: DriverCreate, db: AsyncSession = Depends(get_db)):
    return _out(await fleet_service.create_driver(db, data))


@router.get("", response_model=list[DriverRead])
async def list_drivers(
    page: PageParams, is_active: bool | None = None, db: AsyncSession = Depends(get_db)
):
    rows = await fleet_service.list_drivers(db, is_active, page.limit, page.offset)
    return [_out(driver, truck_id) for driver, truck_id in rows]


@router.get("/{driver_id}", response_model=DriverRead, responses=NOT_FOUND)
async def get_driver(driver_id: int, db: AsyncSession = Depends(get_db)):
    driver, truck_id = await fleet_service.get_driver(db, driver_id)
    return _out(driver, truck_id)


@router.patch(
    "/{driver_id}",
    response_model=DriverRead,
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[Depends(require_manager)],
)
async def update_driver(driver_id: int, data: DriverUpdate, db: AsyncSession = Depends(get_db)):
    await fleet_service.update_driver(db, driver_id, data)
    driver, truck_id = await fleet_service.get_driver(db, driver_id)
    return _out(driver, truck_id)


@router.delete(
    "/{driver_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
    summary="Deactivate a driver (soft delete)",
    description=(
        "The driver row is kept so past transactions still point to it. "
        "The current truck assignment is closed and the driver's active cards are "
        "switched to inactive (blocked cards stay blocked)."
    ),
    dependencies=[Depends(require_manager)],
)
async def deactivate_driver(driver_id: int, db: AsyncSession = Depends(get_db)):
    await fleet_service.deactivate_driver(db, driver_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{driver_id}/reactivate",
    response_model=DriverRead,
    responses=NOT_FOUND,
    summary="Reactivate a driver",
    description=(
        "Undoes a deactivation. Nothing else is restored: the truck assignment stays closed and the "
        "driver's cards stay inactive until a manager assigns a truck and activates the cards again. "
        "Calling it on an active driver changes nothing."
    ),
    dependencies=[Depends(require_manager)],
)
async def reactivate_driver(driver_id: int, db: AsyncSession = Depends(get_db)):
    await fleet_service.reactivate_driver(db, driver_id)
    driver, truck_id = await fleet_service.get_driver(db, driver_id)
    return _out(driver, truck_id)


@router.post(
    "/{driver_id}/assign-truck",
    response_model=AssignmentRead,
    responses={**NOT_FOUND, **CONFLICT, **INVALID_STATE},
    summary="Assign a driver to a truck",
    description=(
        "Closes the driver's current assignment (if any) and opens a new one. "
        "This is also how a manager allows a driver to use a different truck: "
        "purchases with a truck that does not match the assignment are declined."
    ),
    dependencies=[Depends(require_manager)],
)
async def assign_truck(
    driver_id: int, data: AssignTruckRequest, db: AsyncSession = Depends(get_db)
):
    return await fleet_service.assign_truck(db, driver_id, data.truck_id)


@router.post(
    "/{driver_id}/unassign-truck",
    response_model=AssignmentRead,
    responses={**NOT_FOUND, **INVALID_STATE},
    dependencies=[Depends(require_manager)],
)
async def unassign_truck(driver_id: int, db: AsyncSession = Depends(get_db)):
    return await fleet_service.unassign_truck(db, driver_id)


@router.get(
    "/{driver_id}/assignments",
    response_model=list[AssignmentRead],
    responses=NOT_FOUND,
    summary="Truck assignment history of a driver",
)
async def list_assignments(driver_id: int, db: AsyncSession = Depends(get_db)):
    return await fleet_service.list_assignments(db, driver_id)
