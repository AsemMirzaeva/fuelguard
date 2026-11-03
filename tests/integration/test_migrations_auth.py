import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from app.core.config import get_settings
from app.db.base import Base
from app.db.sync_db import sync_engine

settings = get_settings()


class TestMigrations:
    def test_models_and_migrations_agree(self):
        with sync_engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
        assert diff == []

    def test_every_model_table_exists(self):
        assert set(Base.metadata.tables) <= set(inspect(sync_engine).get_table_names())

    def test_downgrade_to_base_and_upgrade_again(self, alembic_config):
        try:
            command.downgrade(alembic_config, "base")
            assert inspect(sync_engine).get_table_names() == ["alembic_version"]
        finally:
            command.upgrade(alembic_config, "head")
        assert set(Base.metadata.tables) <= set(inspect(sync_engine).get_table_names())

    def test_money_and_time_columns_use_exact_types(self):
        columns = {c["name"]: c["type"] for c in inspect(sync_engine).get_columns("fuel_transactions")}
        assert type(columns["total_price"]).__name__ == "NUMERIC"
        assert type(columns["gallons"]).__name__ == "NUMERIC"
        assert columns["transaction_time"].timezone is True

    def test_key_indexes_exist(self):
        names = {i["name"] for i in inspect(sync_engine).get_indexes("fuel_transactions")}
        assert {"ix_fuel_transactions_card_time", "ix_fuel_transactions_truck_time",
                "ix_fuel_transactions_pending"} <= names  # fmt: skip


MANAGER_ENDPOINTS = [
    ("GET", "/drivers"), ("POST", "/drivers"), ("GET", "/trucks"), ("GET", "/cards"),
    ("GET", "/stations"), ("GET", "/transactions"), ("GET", "/transactions/flagged"),
    ("GET", "/transactions/stuck"), ("GET", "/transactions/1"), ("PATCH", "/transactions/1/review"),
    ("GET", "/notifications"), ("GET", "/reports/daily"), ("POST", "/reports/daily/run"),
]  # fmt: skip


class TestAuth:
    @pytest.mark.parametrize("method, path", MANAGER_ENDPOINTS)
    async def test_manager_endpoints_need_a_key(self, anon, method, path):
        r = await anon.request(method, path, json={})
        assert r.status_code == 401
        assert r.json()["error"]["message"] == "Missing API key"

    @pytest.mark.parametrize("method, path", MANAGER_ENDPOINTS)
    async def test_manager_endpoints_reject_a_wrong_key(self, anon, method, path):
        r = await anon.request(method, path, json={}, headers={"X-Manager-Key": "wrong-key-123"})
        assert r.status_code == 403

    async def test_terminal_key_does_not_open_manager_endpoints(self, terminal):
        r = await terminal.get("/drivers", headers={"X-Manager-Key": settings.terminal_api_key})
        assert r.status_code == 403

    async def test_manager_key_does_not_open_the_purchase_endpoint(self, anon):
        r = await anon.post(
            "/transactions",
            json={},
            headers={"X-Terminal-Key": settings.manager_api_key, "Idempotency-Key": "k" * 10},
        )
        assert r.status_code == 403

    async def test_purchase_without_any_key(self, anon):
        r = await anon.post("/transactions", json={}, headers={"Idempotency-Key": "k" * 10})
        assert r.status_code == 401

    async def test_terminal_cannot_list_transactions(self, terminal):
        assert (await terminal.get("/transactions")).status_code == 401

    async def test_health_is_public(self, anon):
        r = await anon.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"


class TestErrorFormatAndDocs:
    async def test_unknown_path_uses_the_error_format(self, anon):
        r = await anon.get("/nope")
        assert r.status_code == 404
        assert set(r.json()["error"]) == {"code", "message", "details"}

    async def test_validation_errors_list_each_field(self, manager):
        r = await manager.post("/drivers", json={"full_name": "", "license_number": "x"})
        body = r.json()["error"]
        assert r.status_code == 422 and body["code"] == "validation_error"
        assert {tuple(d["loc"]) for d in body["details"]} == {
            ("body", "full_name"),
            ("body", "license_number"),
        }

    async def test_unhandled_error_returns_500_without_leaking_details(self, manager_no_raise, monkeypatch):
        async def boom(*a, **kw):
            raise RuntimeError("secret internal detail")

        monkeypatch.setattr("app.services.fleet_service.list_trucks", boom)
        r = await manager_no_raise.get("/trucks")
        assert r.status_code == 500
        assert r.json()["error"] == {
            "code": "internal_error", "message": "Internal server error", "details": None,
        }  # fmt: skip
        assert "secret" not in r.text

    async def test_openapi_documents_security_and_tags(self, anon):
        spec = (await anon.get("/openapi.json")).json()
        assert set(spec["components"]["securitySchemes"]) == {"ManagerKey", "TerminalKey"}
        tags = {t["name"] for t in spec["tags"]}
        assert {"Drivers", "Trucks", "Cards", "Transactions", "Notifications", "Reports"} <= tags

    async def test_purchase_endpoint_documents_idempotency_header_and_errors(self, anon):
        spec = (await anon.get("/openapi.json")).json()
        op = spec["paths"]["/transactions"]["post"]
        assert any(p["name"] == "idempotency-key" and p["required"] for p in op["parameters"])
        assert {"200", "201", "401", "403", "404", "409", "422"} <= set(op["responses"])

    async def test_swagger_ui_is_served(self, anon):
        assert (await anon.get("/docs")).status_code == 200
