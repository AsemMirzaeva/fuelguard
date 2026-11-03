import logging

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import cards, drivers, notifications, reports, stations, transactions, trucks
from app.core.config import get_settings
from app.core.errors import ErrorResponse, register_error_handlers
from app.core.logging import setup_logging
from app.db.async_db import get_db

setup_logging()
logger = logging.getLogger(__name__)

TAGS = [
    {"name": "Health", "description": "Service status"},
    {"name": "Drivers", "description": "Drivers and truck assignment"},
    {"name": "Trucks", "description": "Trucks and tank capacity"},
    {"name": "Cards", "description": "Fuel cards, status and spending limits"},
    {"name": "Stations", "description": "Fuel stations"},
    {"name": "Transactions", "description": "Fuel purchases and review of flagged ones"},
    {"name": "Notifications", "description": "Alerts for suspicious activity"},
    {"name": "Reports", "description": "Daily fuel expense reports"},
]

app = FastAPI(
    title="FuelGuard",
    description=(
        "Fuel card management, fuel purchases and risk detection for a trucking company.\n\n"
        "Manager endpoints need the `X-Manager-Key` header, the purchase endpoint needs "
        "`X-Terminal-Key`. Use the Authorize button to set them."
    ),
    version="0.1.0",
    openapi_tags=TAGS,
    responses={
        422: {"model": ErrorResponse, "description": "Validation error"},
    },
)
register_error_handlers(app)
app.include_router(drivers.router)
app.include_router(trucks.router)
app.include_router(cards.router)
app.include_router(stations.router)
app.include_router(transactions.router)
app.include_router(notifications.router)
app.include_router(reports.router)


@app.get("/health", tags=["Health"])
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "env": get_settings().app_env}
