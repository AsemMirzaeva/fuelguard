from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

sync_engine = create_engine(get_settings().database_url_sync, pool_pre_ping=True)
SessionLocal = sessionmaker(sync_engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Session for Celery workers: commit on success, roll back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
