import logging

import jwt
import pytest
from fastapi import HTTPException

from app.core import security
from app.core.config import Settings
from app.core.security import Principal
from tests import tokens
from tests.tokens import (
    OTHER_PRIVATE_KEY,
    UnreachableJwksClient,
    hmac_token_signed_with_public_key,
    make_token,
    unsigned_token,
)

# Captured at import time, before the "jwks" fixture swaps in a stand-in.
REAL_GET_JWKS_CLIENT = security.get_jwks_client

pytestmark = pytest.mark.usefixtures("jwks")


async def rejected(token: str, status: int = 401) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await security.verify_token(token)
    assert caught.value.status_code == status
    return caught.value


class TestAcceptedTokens:
    async def test_valid_token_gives_a_principal(self):
        p = await security.verify_token(make_token(["manager"], username="alice", subject="u-1"))
        assert p == Principal(subject="u-1", username="alice", roles=frozenset({"manager"}))

    async def test_several_roles(self):
        p = await security.verify_token(make_token(["manager", "auditor", "offline_access"]))
        assert {"manager", "auditor"} <= p.roles

    async def test_username_falls_back_to_the_subject(self):
        p = await security.verify_token(make_token(["terminal"], username=None, subject="svc-9"))
        assert p.username == "svc-9"

    async def test_audience_may_be_a_list_that_includes_ours(self):
        token = make_token(audience=["account", tokens.settings.oidc_audience])
        assert (await security.verify_token(token)).roles == {"manager"}

    async def test_small_clock_differences_are_tolerated(self):
        assert await security.verify_token(make_token(expires_in=-5))  # expired 5 s ago, leeway 10 s

    @pytest.mark.parametrize(
        "realm_access", [None, "manager", {"roles": "manager"}, {"roles": [1, None, "auditor"]}, {}]
    )
    async def test_odd_role_claims_never_grant_roles_by_accident(self, realm_access):
        token = make_token(drop=("realm_access",), extra={"realm_access": realm_access})
        assert (await security.verify_token(token)).roles <= {"auditor"}


class TestRejectedTokens:
    async def test_expired(self):
        error = await rejected(make_token(expires_in=-60))
        assert error.detail == "Access token has expired"
        assert error.headers == {"WWW-Authenticate": "Bearer"}

    async def test_not_yet_valid(self):
        await rejected(make_token(extra={"nbf": 4_102_444_800}))  # the year 2100

    async def test_wrong_issuer(self):
        await rejected(make_token(issuer="http://evil.example/realms/fuelguard"))

    async def test_wrong_audience(self):
        await rejected(make_token(audience="some-other-api"))

    @pytest.mark.parametrize("claim", ["exp", "iss", "aud", "sub"])
    async def test_required_claims_must_be_present(self, claim):
        await rejected(make_token(drop=(claim,)))

    async def test_signature_from_a_different_key(self):
        await rejected(make_token(key=OTHER_PRIVATE_KEY))  # same key id, forged signature

    async def test_tampered_payload(self):
        header, payload, signature = make_token(["auditor"]).split(".")
        forged = make_token(["manager"]).split(".")[1]
        await rejected(f"{header}.{forged}.{signature}")

    async def test_unknown_key_id(self):
        await rejected(make_token(kid="somebody-elses-key"))

    async def test_missing_key_id(self):
        await rejected(make_token(kid=None))

    async def test_algorithm_none_is_refused(self):
        await rejected(unsigned_token())

    async def test_algorithm_confusion_attack_is_refused(self):
        """HS256 signed with the public key as the secret must not be accepted as RS256."""
        await rejected(hmac_token_signed_with_public_key())

    @pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "not.a.jwt.at.all", "Bearer x"])
    async def test_garbage(self, garbage):
        await rejected(garbage)

    async def test_the_reason_is_not_revealed_to_the_caller(self):
        details = {
            (await rejected(make_token(issuer="http://x"))).detail,
            (await rejected(make_token(audience="y"))).detail,
            (await rejected(make_token(key=OTHER_PRIVATE_KEY))).detail,
        }
        assert details == {"Invalid access token"}

    async def test_the_reason_is_logged(self, caplog):
        with caplog.at_level(logging.INFO):
            await rejected(make_token(audience="y"))
        assert "InvalidAudienceError" in caplog.text


class TestIdentityProviderOutage:
    async def test_unreachable_provider_is_a_503_not_a_401(self, monkeypatch, caplog):
        monkeypatch.setattr(security, "get_jwks_client", lambda: UnreachableJwksClient())
        with caplog.at_level(logging.ERROR):
            error = await rejected(make_token(), status=503)
        assert error.detail == "Identity provider is not available"
        assert "Cannot reach the identity provider" in caplog.text


class TestRoleChecks:
    def principal(self, *roles):
        return Principal("s", "u", frozenset(roles))

    @pytest.mark.parametrize(
        "check, role, allowed",
        [
            (security.require_reader, "manager", True),
            (security.require_reader, "auditor", True),
            (security.require_reader, "terminal", False),
            (security.require_manager, "manager", True),
            (security.require_manager, "auditor", False),
            (security.require_manager, "terminal", False),
            (security.require_terminal, "terminal", True),
            (security.require_terminal, "manager", False),
            (security.require_terminal, "auditor", False),
        ],
    )
    def test_role_matrix(self, check, role, allowed):
        p = self.principal(role)
        if allowed:
            assert check(p) is p
        else:
            with pytest.raises(HTTPException) as caught:
                check(p)
            assert caught.value.status_code == 403
            assert "Requires one of the roles" in caught.value.detail

    def test_no_roles_means_no_access(self):
        for check in (security.require_reader, security.require_manager, security.require_terminal):
            with pytest.raises(HTTPException):
                check(self.principal())

    def test_unrelated_roles_do_not_help(self):
        with pytest.raises(HTTPException):
            security.require_manager(self.principal("offline_access", "uma_authorization"))


class TestJwksClient:
    def test_one_shared_client_built_from_the_settings(self, monkeypatch):
        created = []

        class Spy:
            def __init__(self, uri, **kwargs):
                created.append((uri, kwargs))

        monkeypatch.setattr(security, "_jwks_client", None)
        monkeypatch.setattr(security, "PyJWKClient", Spy)

        first, second = REAL_GET_JWKS_CLIENT(), REAL_GET_JWKS_CLIENT()

        assert first is second and len(created) == 1  # keys are cached, not re-fetched per request
        uri, options = created[0]
        assert uri == tokens.settings.jwks_url
        assert options == {"cache_keys": True, "lifespan": 300, "timeout": 5}


class TestSettings:
    ENV = dict(
        database_url_async="postgresql+asyncpg://x",
        database_url_sync="postgresql+psycopg2://x",
        redis_url="redis://x",
        oidc_issuer="http://localhost:8180/realms/fuelguard/",
    )

    @pytest.fixture(autouse=True)
    def _ignore_container_settings(self, monkeypatch):
        # The container sets these for the real app; these tests check the defaults.
        monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)

    def test_issuer_trailing_slash_is_removed(self):
        assert Settings(_env_file=None, **self.ENV).oidc_issuer == "http://localhost:8180/realms/fuelguard"

    def test_urls_are_derived_from_the_issuer(self):
        s = Settings(_env_file=None, **self.ENV)
        base = "http://localhost:8180/realms/fuelguard/protocol/openid-connect"
        assert (s.oidc_authorization_url, s.oidc_token_url, s.jwks_url) == (
            f"{base}/auth", f"{base}/token", f"{base}/certs",
        )  # fmt: skip

    def test_jwks_url_can_point_at_the_internal_address(self):
        s = Settings(_env_file=None, **self.ENV, oidc_jwks_url="http://keycloak:8080/certs")
        assert s.jwks_url == "http://keycloak:8080/certs"
        assert s.oidc_token_url.startswith("http://localhost:8180")  # browsers still use the issuer

    def test_audience_defaults_to_the_api_name(self):
        assert Settings(_env_file=None, **self.ENV).oidc_audience == "fuelguard-api"
