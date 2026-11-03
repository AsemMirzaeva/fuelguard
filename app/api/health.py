import asyncio
import json
import logging
import urllib.request
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


def _fetch_signing_keys(url: str) -> bool:
    with urllib.request.urlopen(url, timeout=2) as response:  # the URL comes from our own settings
        return response.status == 200 and bool(json.load(response).get("keys"))


async def check_identity_provider() -> bool:
    """Can the API fetch the token signing keys? If not, no request can be authenticated."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_fetch_signing_keys, get_settings().jwks_url), CHECK_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning("Health check: identity provider is not reachable", exc_info=True)
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
        "Checks PostgreSQL, Redis and the identity provider (can the token signing keys be "
        "fetched?), the three things the API cannot work without. Returns 200 when all answer "
        "and 503 when any does not. It does not check that a Celery worker is running: "
        "unfinished background work shows up in `GET /transactions/stuck`."
    ),
)
async def health(response: Response, db: AsyncSession = Depends(get_db)) -> HealthResponse:
    results = await asyncio.gather(
        check_database(db), check_redis(), check_identity_provider()
    )
    names = ("database", "redis", "identity_provider")
    checks = {name: "ok" if ok else "error" for name, ok in zip(names, results)}
    healthy = all(results)
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if healthy else "degraded", env=get_settings().app_env, checks=checks
    )
