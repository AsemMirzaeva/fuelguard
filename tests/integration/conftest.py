import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.sync_db import SessionLocal

settings = get_settings()


@pytest.fixture(autouse=True)
def _fresh_database(clean_db):
    """Every integration test starts from empty tables."""


@pytest.fixture(autouse=True)
def queued(monkeypatch):
    """Replace the queue with a list, so no test can publish to the real broker.

    Each entry is (task_name, args).
    """
    calls: list[tuple] = []

    async def fake_enqueue(name, *args):
        calls.append((name, args))
        return True

    monkeypatch.setattr("app.api.transactions.enqueue", fake_enqueue)
    monkeypatch.setattr("app.api.reports.enqueue", fake_enqueue)
    return calls


def _client(headers=None, raise_app_exceptions=True) -> AsyncClient:
    from app.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers or {})


@pytest_asyncio.fixture
async def manager():
    async with _client({"X-Manager-Key": settings.manager_api_key}) as c:
        yield c


@pytest_asyncio.fixture
async def terminal():
    async with _client({"X-Terminal-Key": settings.terminal_api_key}) as c:
        yield c


@pytest_asyncio.fixture
async def terminal_no_raise():
    async with _client({"X-Terminal-Key": settings.terminal_api_key}, raise_app_exceptions=False) as c:
        yield c


@pytest_asyncio.fixture
async def anon():
    async with _client() as c:
        yield c


@pytest_asyncio.fixture
async def manager_no_raise():
    """Returns 500 responses instead of re-raising unhandled app errors."""
    async with _client({"X-Manager-Key": settings.manager_api_key}, raise_app_exceptions=False) as c:
        yield c


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
