from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FuelStation
from app.schemas.transactions import StationCreate
from app.services.common import get_or_404


async def create_station(db: AsyncSession, data: StationCreate) -> FuelStation:
    station = FuelStation(**data.model_dump())
    db.add(station)
    await db.commit()
    return station


async def list_stations(db: AsyncSession, limit: int, offset: int) -> list[FuelStation]:
    stmt = select(FuelStation).order_by(FuelStation.id).limit(limit).offset(offset)
    return list((await db.execute(stmt)).scalars())


async def get_station(db: AsyncSession, station_id: int) -> FuelStation:
    return await get_or_404(db, FuelStation, station_id, "Station")
