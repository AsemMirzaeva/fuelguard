import re

import pytest

from app.core.config import get_settings
from app.main import app
from tests.factories import create_card, create_driver, make_world
from tests.tokens import (
    OTHER_PRIVATE_KEY,
    UnreachableJwksClient,
    hmac_token_signed_with_public_key,
    make_token,
    unsigned_token,
)

settings = get_settings()


def protected_routes() -> list[tuple[str, str, str]]:
    """Every operation in the served API except /health, as (method, path, kind).

    Built from the OpenAPI spec, so a new endpoint is covered automatically and cannot be
    left open by accident."""
    found = []
    for path, operations in app.openapi()["paths"].items():
        if path == "/health":
            continue
        for method in sorted(operations):
            if (method, path) == ("post", "/transactions"):
                kind = "terminal"
            elif method == "get":
                kind = "read"
            else:
                kind = "write"
            found.append((method.upper(), re.sub(r"\{[^}]+\}", "1", path), kind))
    # An empty list would make pytest silently skip every test below, hiding the gap.
    assert found, "no routes found: the role matrix would test nothing"
    return found


ROUTES = protected_routes()
ALLOWED = {  # which kinds of route each role may call
    "manager": {"read", "write"},
    "auditor": {"read"},
    "terminal": {"terminal"},
}


def test_the_matrix_covers_every_endpoint():
    assert len(ROUTES) == 34  # 35 operations minus /health
    assert {k for *_, k in ROUTES} == {"read", "write", "terminal"}


@pytest.mark.parametrize("method, path, kind", ROUTES)
class TestEveryEndpoint:
    async def test_no_token_is_401(self, anon, method, path, kind):
        r = await anon.request(method, path, json={})
        assert r.status_code == 401
        assert r.json()["error"]["message"] == "Missing access token"
        assert r.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("role", ["manager", "auditor", "terminal"])
    async def test_role_decides_access(self, client_with, method, path, kind, role):
        client = client_with(make_token([role]))
        r = await client.request(method, path, json={})
        if kind in ALLOWED[role]:
            assert r.status_code not in (401, 403), f"{role} should be let in: {r.text}"
        else:
            assert r.status_code == 403, f"{role} should be refused: {r.status_code} {r.text}"
            assert "Requires one of the roles" in r.json()["error"]["message"]

    async def test_token_without_any_known_role_is_403(self, client_with, method, path, kind):
        client = client_with(make_token(["offline_access"]))
        assert (await client.request(method, path, json={})).status_code == 403


class TestTokenProblemsOverHttp:
    async def test_expired_token(self, client_with):
        r = await client_with(make_token(expires_in=-60)).get("/drivers")
        assert r.status_code == 401 and r.json()["error"]["message"] == "Access token has expired"

    @pytest.mark.parametrize(
        "token",
        [
            make_token(issuer="http://evil.example/realms/fuelguard"),
            make_token(audience="another-api"),
            make_token(key=OTHER_PRIVATE_KEY),
            make_token(kid="unknown-key"),
            unsigned_token(),
            hmac_token_signed_with_public_key(),
            "not-a-token",
        ],
        ids=["issuer", "audience", "forged", "unknown-kid", "alg-none", "alg-confusion", "garbage"],
    )
    async def test_bad_tokens_are_401_with_a_generic_message(self, client_with, token):
        r = await client_with(token).get("/drivers")
        assert r.status_code == 401
        assert r.json()["error"]["message"] == "Invalid access token"
        assert r.headers["www-authenticate"] == "Bearer"

    @pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Token abc", "Bearer", ""])
    async def test_other_authorization_schemes_are_ignored(self, anon, header):
        r = await anon.get("/drivers", headers={"Authorization": header})
        assert r.status_code == 401 and r.json()["error"]["message"] == "Missing access token"

    async def test_the_old_api_key_headers_no_longer_work(self, anon):
        r = await anon.get("/drivers", headers={"X-Manager-Key": "anything-at-all"})
        assert r.status_code == 401

    async def test_identity_provider_down_is_503_not_401(self, client_with, monkeypatch):
        from app.core import security

        monkeypatch.setattr(security, "get_jwks_client", lambda: UnreachableJwksClient())
        r = await client_with(make_token()).get("/drivers")
        assert r.status_code == 503
        assert r.json()["error"]["message"] == "Identity provider is not available"

    async def test_public_endpoints_need_no_token(self, anon):
        for path in ("/health", "/docs", "/openapi.json", "/redoc"):
            assert (await anon.get(path)).status_code == 200


class TestAuditorIsReadOnly:
    async def test_can_read_but_changes_nothing(self, manager, auditor):
        w = await make_world(manager)
        assert (await auditor.get("/cards")).status_code == 200
        assert (await auditor.get(f"/drivers/{w.driver_id}")).status_code == 200
        assert (await auditor.get("/transactions/flagged")).status_code == 200
        assert (await auditor.get("/reports/daily")).status_code == 200

        r = await auditor.patch(f"/cards/{w.card_id}/status", json={"status": "blocked", "comment": "test"})
        assert r.status_code == 403
        assert (await manager.get(f"/cards/{w.card_id}")).json()["status"] == "active"  # unchanged
        assert (await auditor.delete(f"/drivers/{w.driver_id}")).status_code == 403
        assert (await manager.get(f"/drivers/{w.driver_id}")).json()["is_active"] is True

    async def test_cannot_review_or_run_reports(self, auditor):
        assert (await auditor.patch("/transactions/1/review", json={"review_status": "reviewed"})).status_code == 403
        assert (await auditor.post("/reports/daily/run")).status_code == 403


class TestTerminalIsPurchaseOnly:
    async def test_can_purchase_but_not_read_anything(self, manager, terminal):
        from tests.factories import purchase

        w = await make_world(manager)
        assert (await purchase(terminal, w)).status_code == 201
        for path in ("/transactions", "/cards", "/drivers", "/notifications", "/reports/daily"):
            assert (await terminal.get(path)).status_code == 403

    async def test_a_manager_cannot_send_purchases(self, manager):
        from tests.factories import purchase

        w = await make_world(manager)
        r = await purchase(manager, w)
        assert r.status_code == 403 and "terminal" in r.json()["error"]["message"]


class TestAuditTrail:
    async def test_card_history_names_the_person_who_acted(self, client_with, manager):
        alice = client_with(make_token(["manager"], username="alice"))
        bob = client_with(make_token(["manager"], username="bob"))
        driver = await create_driver(manager)

        card = await create_card(alice, driver["id"])
        await bob.patch(f"/cards/{card['id']}/status", json={"status": "blocked", "comment": "test"})
        await alice.patch(f"/cards/{card['id']}/status", json={"status": "active", "comment": "ok"})

        history = (await manager.get(f"/cards/{card['id']}/history")).json()
        assert [(h["new_status"], h["changed_by"]) for h in history] == [
            ("inactive", "alice"), ("active", "alice"), ("blocked", "bob"), ("active", "alice"),
        ]  # fmt: skip

    async def test_falls_back_to_the_subject_when_there_is_no_username(self, client_with, manager):
        anonymous_user = client_with(make_token(["manager"], username=None, subject="sub-123"))
        driver = await create_driver(manager)
        card = await create_card(anonymous_user, driver["id"])
        assert (await manager.get(f"/cards/{card['id']}/history")).json()[0]["changed_by"] == "sub-123"


class TestSwaggerLogin:
    async def test_both_login_methods_are_described_in_the_spec(self, anon):
        schemes = (await anon.get("/openapi.json")).json()["components"]["securitySchemes"]
        flow = schemes["ManagerLogin"]["flows"]["authorizationCode"]
        assert flow["authorizationUrl"] == settings.oidc_authorization_url
        assert flow["tokenUrl"] == settings.oidc_token_url
        assert schemes["TerminalClient"]["flows"]["clientCredentials"]["tokenUrl"] == settings.oidc_token_url

    async def test_every_protected_endpoint_declares_a_security_scheme(self, anon):
        spec = (await anon.get("/openapi.json")).json()
        for path, operations in spec["paths"].items():
            for method, operation in operations.items():
                if path == "/health":
                    assert "security" not in operation
                else:
                    assert operation.get("security"), f"{method.upper()} {path} has no lock"

    async def test_purchase_uses_the_terminal_login_and_the_rest_the_manager_login(self, anon):
        spec = (await anon.get("/openapi.json")).json()["paths"]
        schemes = lambda method, path: {k for req in spec[path][method]["security"] for k in req}  # noqa: E731
        assert schemes("post", "/transactions") == {"TerminalClient"}
        assert schemes("get", "/transactions") == {"ManagerLogin"}
        assert schemes("patch", "/cards/{card_id}/status") == {"ManagerLogin"}

    async def test_swagger_is_set_up_to_log_in_with_pkce(self, anon):
        page = (await anon.get("/docs")).text
        assert "fuelguard-manager" in page and "usePkceWithAuthorizationCodeGrant" in page
        assert (await anon.get("/docs/oauth2-redirect")).status_code == 200

    async def test_every_error_response_is_documented_for_protected_endpoints(self, anon):
        op = (await anon.get("/openapi.json")).json()["paths"]["/drivers"]["get"]
        assert {"401", "403", "503"} <= set(op["responses"])
