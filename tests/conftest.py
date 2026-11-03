from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
settings = get_settings()


def _admin_engine():
    url = make_url(settings.database_url_sync).set(database="postgres")
    return create_engine(url, isolation_level="AUTOCOMMIT")


# Tables are truncated between tests, so refuse to run against anything but a *_test database.
_db_name = make_url(settings.database_url_sync).database or ""
if not _db_name.endswith("_test"):
    pytest.exit(
        f"Refusing to run: database '{_db_name}' is not a test database. "
        "Run the tests with: docker compose run --rm --build tests",
        returncode=2,
    )


@pytest.fixture(scope="session")
def _database():
    """Create the test database if needed and build its schema from the Alembic migrations."""
    with _admin_engine().connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": _db_name}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{_db_name}"'))

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    return config


@pytest.fixture
def alembic_config(_database) -> Config:
    return _database


@pytest.fixture
def clean_db(_database):
    from app.db.base import Base
    from app.db.sync_db import sync_engine

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def jwks(monkeypatch):
    """Replace the identity provider's key lookup with one that knows the test signing key."""
    from app.core import security
    from tests.tokens import FakeJwksClient

    client = FakeJwksClient()
    monkeypatch.setattr(security, "get_jwks_client", lambda: client)
    return client
