from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PageParams
from app.core.errors import AUTH_ERRORS, NOT_FOUND
from app.core.security import require_manager
from app.db.async_db import get_db
from app.schemas.transactions import StationCreate, StationRead
from app.services import station_service

router = APIRouter(
    prefix="/stations",
    tags=["Stations"],
    dependencies=[Depends(require_manager)],
    responses=AUTH_ERRORS,
)


@router.post("", response_model=StationRead, status_code=status.HTTP_201_CREATED)
async def create_station(data: StationCreate, db: AsyncSession = Depends(get_db)):
    return await station_service.create_station(db, data)


@router.get("", response_model=list[StationRead])
async def list_stations(page: PageParams, db: AsyncSession = Depends(get_db)):
    return await station_service.list_stations(db, page.limit, page.offset)


@router.get("/{station_id}", response_model=StationRead, responses=NOT_FOUND)
async def get_station(station_id: int, db: AsyncSession = Depends(get_db)):
    return await station_service.get_station(db, station_id)
