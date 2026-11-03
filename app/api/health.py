import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.async_db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

CHECK_TIMEOUT_SECONDS = 3


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    env: str
    checks: dict[str, Literal["ok", "error"]]


async def check_database(db: AsyncSession) -> bool:
    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), CHECK_TIMEOUT_SECONDS)
        return True
    except Exception:
        logger.warning("Health check: database is not reachable", exc_info=True)
        return False


async def check_redis() -> bool:
    client = Redis.from_url(
        get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        await asyncio.wait_for(client.ping(), CHECK_TIMEOUT_SECONDS)
        return True
    except Exception:
        logger.warning("Health check: Redis is not reachable", exc_info=True)
        return False
    finally:
        await client.aclose()


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "A dependency is not reachable"}},
    summary="Is the service up and are its dependencies reachable?",
    description=(
        "Checks PostgreSQL and Redis, the two things the API cannot work without. "
        "Returns 200 when both answer and 503 when either does not. "
        "It does not check that a Celery worker is running: unfinished background work "
        "shows up in `GET /transactions/stuck`."
    ),
)
async def health(response: Response, db: AsyncSession = Depends(get_db)) -> HealthResponse:
    database_ok, redis_ok = await asyncio.gather(check_database(db), check_redis())
    checks = {"database": "ok" if database_ok else "error", "redis": "ok" if redis_ok else "error"}
    healthy = database_ok and redis_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if healthy else "degraded", env=get_settings().app_env, checks=checks
    )
