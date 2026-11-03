import asyncio
import logging

import pytest

from app.api import health

# Captured at import time, before any fixture replaces it with a stand-in.
REAL_CHECK_REDIS = health.check_redis


async def broken(*args, **kwargs) -> bool:
    return False


class TestHealthEndpoint:
    async def test_ok_when_database_and_redis_answer(self, anon):
        r = await anon.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "env": "local", "checks": {"database": "ok", "redis": "ok"}}

    async def test_needs_no_api_key(self, anon):
        assert (await anon.get("/health")).status_code == 200

    async def test_503_when_redis_is_down(self, anon, monkeypatch):
        monkeypatch.setattr(health, "check_redis", broken)
        r = await anon.get("/health")
        assert r.status_code == 503
        assert r.json()["status"] == "degraded"
        assert r.json()["checks"] == {"database": "ok", "redis": "error"}

    async def test_503_when_the_database_is_down(self, anon, monkeypatch):
        monkeypatch.setattr(health, "check_database", broken)
        r = await anon.get("/health")
        assert r.status_code == 503
        assert r.json()["checks"] == {"database": "error", "redis": "ok"}

    async def test_both_down(self, anon, monkeypatch):
        monkeypatch.setattr(health, "check_database", broken)
        monkeypatch.setattr(health, "check_redis", broken)
        r = await anon.get("/health")
        assert r.status_code == 503 and r.json()["checks"] == {"database": "error", "redis": "error"}

    async def test_a_down_dependency_does_not_leak_details(self, anon, monkeypatch):
        monkeypatch.setattr(health, "check_redis", broken)
        assert "redis://" not in (await anon.get("/health")).text

    async def test_documented_in_openapi(self, anon):
        op = (await anon.get("/openapi.json")).json()["paths"]["/health"]["get"]
        assert {"200", "503"} <= set(op["responses"])


class TestCheckDatabase:
    async def test_true_for_a_working_connection(self):
        from app.db.async_db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            assert await health.check_database(session) is True

    async def test_false_and_logged_when_the_query_fails(self, caplog):
        class Broken:
            async def execute(self, *a, **kw):
                raise ConnectionError("connection refused")

        with caplog.at_level(logging.WARNING):
            assert await health.check_database(Broken()) is False
        assert "database is not reachable" in caplog.text

    async def test_false_when_the_query_hangs(self, monkeypatch):
        monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)

        class Hanging:
            async def execute(self, *a, **kw):
                await asyncio.sleep(5)

        assert await health.check_database(Hanging()) is False


class FakeRedis:
    def __init__(self, ping_error=None, ping_delay=0):
        self.ping_error, self.ping_delay, self.closed = ping_error, ping_delay, False

    async def ping(self):
        await asyncio.sleep(self.ping_delay)
        if self.ping_error:
            raise self.ping_error
        return True

    async def aclose(self):
        self.closed = True


class TestCheckRedis:
    @pytest.fixture(autouse=True)
    def real_check(self, monkeypatch):
        # undo the autouse stand-in from conftest so the real function is exercised
        monkeypatch.setattr(health, "check_redis", REAL_CHECK_REDIS)

    def use(self, monkeypatch, client):
        monkeypatch.setattr(health.Redis, "from_url", lambda url, **kw: client)
        return client

    async def test_true_when_redis_answers_and_the_client_is_closed(self, monkeypatch):
        client = self.use(monkeypatch, FakeRedis())
        assert await health.check_redis() is True and client.closed

    async def test_false_and_logged_when_redis_refuses(self, monkeypatch, caplog):
        client = self.use(monkeypatch, FakeRedis(ping_error=ConnectionError("refused")))
        with caplog.at_level(logging.WARNING):
            assert await health.check_redis() is False
        assert "Redis is not reachable" in caplog.text and client.closed

    async def test_false_when_redis_hangs(self, monkeypatch):
        monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)
        client = self.use(monkeypatch, FakeRedis(ping_delay=5))
        assert await health.check_redis() is False and client.closed

    async def test_a_really_unreachable_address_is_reported_down_quickly(self):
        # The test container's REDIS_URL points at a closed port on purpose.
        assert await asyncio.wait_for(health.check_redis(), 6) is False
