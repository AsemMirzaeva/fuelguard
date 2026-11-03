import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db.sync_db import SessionLocal
from tests.tokens import bearer, make_token


@pytest.fixture(autouse=True)
def _fresh_database(clean_db):
    """Every integration test starts from empty tables."""


@pytest.fixture(autouse=True)
def _signing_keys(jwks):
    """Tokens in these tests are signed with a throwaway key the API is told to trust."""


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


@pytest.fixture(autouse=True)
def _dependencies_report_healthy(monkeypatch):
    """The test container has no Redis or Keycloak (their addresses point nowhere on purpose),
    so /health would report them as down. Tests of the health check itself override this."""

    async def healthy(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr("app.api.health.check_redis", healthy)
    monkeypatch.setattr("app.api.health.check_identity_provider", healthy)


def _client(headers=None, raise_app_exceptions=True) -> AsyncClient:
    from app.main import app

    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers or {})


@pytest_asyncio.fixture
async def manager():
    async with _client(bearer(make_token(["manager"]))) as c:
        yield c


@pytest_asyncio.fixture
async def auditor():
    async with _client(bearer(make_token(["auditor"], username="auditor.test"))) as c:
        yield c


@pytest_asyncio.fixture
async def terminal():
    token = make_token(["terminal"], username="service-account-fuelguard-terminal")
    async with _client(bearer(token)) as c:
        yield c


@pytest_asyncio.fixture
async def anon():
    async with _client() as c:
        yield c


@pytest_asyncio.fixture
async def client_with():
    """Build a client that sends a given token, for tests that need an unusual one."""
    opened: list[AsyncClient] = []

    def build(token: str | None, raise_app_exceptions: bool = True) -> AsyncClient:
        client = _client(bearer(token) if token else None, raise_app_exceptions)
        opened.append(client)
        return client

    yield build
    for client in opened:
        await client.aclose()


@pytest_asyncio.fixture
async def manager_no_raise():
    """Returns 500 responses instead of re-raising unhandled app errors."""
    async with _client(bearer(make_token(["manager"])), raise_app_exceptions=False) as c:
        yield c


@pytest_asyncio.fixture
async def terminal_no_raise():
    token = make_token(["terminal"], username="service-account-fuelguard-terminal")
    async with _client(bearer(token), raise_app_exceptions=False) as c:
        yield c


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session

