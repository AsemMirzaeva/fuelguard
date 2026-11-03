from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError

T = TypeVar("T")


async def get_or_404(
    db: AsyncSession, model: type[T], obj_id: int, label: str, lock: bool = False
) -> T:
    if lock:
        stmt = select(model).where(model.id == obj_id).with_for_update()  # type: ignore[attr-defined]
        obj = (await db.execute(stmt)).scalar_one_or_none()
    else:
        obj = await db.get(model, obj_id)
    if obj is None:
        raise NotFoundError(f"{label} {obj_id} not found")
    return obj


@asynccontextmanager
async def commit_or_conflict(db: AsyncSession, message: str) -> AsyncIterator[None]:
    """Commit at the end of the block; a violated unique constraint becomes a 409."""
    try:
        yield
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(message) from exc
