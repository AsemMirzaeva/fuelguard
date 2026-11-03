from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageParams
from app.core.errors import AUTH_ERRORS, CONFLICT, NOT_FOUND
from app.core.security import require_manager, require_reader
from app.db.async_db import get_db
from app.schemas.fleet import TruckCreate, TruckRead, TruckUpdate
from app.services import fleet_service

router = APIRouter(
    prefix="/trucks",
    tags=["Trucks"],
    dependencies=[Depends(require_reader)],
    responses=AUTH_ERRORS,
)


@router.post(
    "",
    response_model=TruckRead,
    status_code=status.HTTP_201_CREATED,
    responses=CONFLICT,
    dependencies=[Depends(require_manager)],
)
async def create_truck(data: TruckCreate, db: AsyncSession = Depends(get_db)):
    return await fleet_service.create_truck(db, data)


@router.get("", response_model=list[TruckRead])
async def list_trucks(
    page: PageParams, is_active: bool | None = None, db: AsyncSession = Depends(get_db)
):
    return await fleet_service.list_trucks(db, is_active, page.limit, page.offset)


@router.get("/{truck_id}", response_model=TruckRead, responses=NOT_FOUND)
async def get_truck(truck_id: int, db: AsyncSession = Depends(get_db)):
    return await fleet_service.get_truck(db, truck_id)


@router.patch(
    "/{truck_id}",
    response_model=TruckRead,
    responses={**NOT_FOUND, **CONFLICT},
    dependencies=[Depends(require_manager)],
)
async def update_truck(truck_id: int, data: TruckUpdate, db: AsyncSession = Depends(get_db)):
    return await fleet_service.update_truck(db, truck_id, data)


@router.delete(
    "/{truck_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
    summary="Deactivate a truck (soft delete)",
    description="The truck row is kept for history. Its current driver assignments are closed.",
    dependencies=[Depends(require_manager)],
)
async def deactivate_truck(truck_id: int, db: AsyncSession = Depends(get_db)):
    await fleet_service.deactivate_truck(db, truck_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
