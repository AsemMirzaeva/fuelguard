"""OAuth2 bearer-token authentication and role checks.

Tokens are issued by the identity provider (Keycloak, OpenID Connect). This module only
verifies them. A token is accepted when:

* its signature is valid RS256 with a key from the provider's JWKS (matched by key id),
* it has not expired and was issued by the expected issuer,
* it was meant for this API (audience), and
* for the endpoint's role check, it carries one of the allowed realm roles.

Roles: ``manager`` (full access), ``auditor`` (read-only), ``terminal`` (purchases only).
"""

import asyncio
import logging
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.openapi.models import OAuthFlows
from fastapi.security import OAuth2, OAuth2AuthorizationCodeBearer
from fastapi.security.utils import get_authorization_scheme_param
from jwt import PyJWKClient

from app.core.config import get_settings

logger = logging.getLogger(__name__)

MANAGER = "manager"
AUDITOR = "auditor"
TERMINAL = "terminal"

ALGORITHMS = ["RS256"]  # never "none", never a shared-secret algorithm
CLOCK_LEEWAY_SECONDS = 10

_settings = get_settings()

# Two schemes so that Swagger's Authorize button offers the right login for each kind of caller.
# Both read the same ``Authorization: Bearer <token>`` header.
manager_login = OAuth2AuthorizationCodeBearer(
    authorizationUrl=_settings.oidc_authorization_url,
    tokenUrl=_settings.oidc_token_url,
    scheme_name="ManagerLogin",
    description="Log in as a person (manager or auditor). Uses the authorization code flow with PKCE.",
    auto_error=False,
)


class _ClientCredentialsBearer(OAuth2):
    """Shows the client credentials flow in Swagger; returns the bearer token from the header."""

    async def __call__(self, request: Request) -> str | None:
        scheme, token = get_authorization_scheme_param(request.headers.get("Authorization"))
        return token if scheme.lower() == "bearer" and token else None


terminal_client = _ClientCredentialsBearer(
    flows=OAuthFlows(clientCredentials={"tokenUrl": _settings.oidc_token_url}),
    scheme_name="TerminalClient",
    description="Card terminal. Uses the client credentials flow (client id and secret).",
    auto_error=False,
)


@dataclass(frozen=True)
class Principal:
    """Who is calling: taken from a verified token."""

    subject: str
    username: str
    roles: frozenset[str]


_jwks_client: PyJWKClient | None = None


def get_jwks_client() -> PyJWKClient:
    """One shared client: it caches the provider's keys and re-reads them for an unknown key id."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(get_settings().jwks_url, cache_keys=True, lifespan=300, timeout=5)
    return _jwks_client


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail=message, headers={"WWW-Authenticate": "Bearer"}
    )


async def verify_token(token: str) -> Principal:
    settings = get_settings()
    try:
        # The key lookup may make a network call, so keep it off the event loop.
        key = await asyncio.to_thread(get_jwks_client().get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=ALGORITHMS,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            leeway=CLOCK_LEEWAY_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWKClientConnectionError:
        # Not the caller's fault: we cannot reach the identity provider to check the token.
        logger.error("Cannot reach the identity provider to fetch signing keys", exc_info=True)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Identity provider is not available")
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Access token has expired")
    except (jwt.InvalidTokenError, jwt.PyJWKClientError) as exc:
        # Do not tell the caller which check failed; keep that for the log.
        logger.info("Rejected access token: %s: %s", type(exc).__name__, exc)
        raise _unauthorized("Invalid access token")

    realm_access = claims.get("realm_access")
    raw_roles = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
    roles = frozenset(r for r in raw_roles if isinstance(r, str))
    subject = str(claims["sub"])
    return Principal(subject, claims.get("preferred_username") or subject, roles)


def _authenticate(scheme):
    async def authenticated(token: str | None = Security(scheme)) -> Principal:
        if not token:
            raise _unauthorized("Missing access token")
        return await verify_token(token)

    return authenticated


_current_user = _authenticate(manager_login)
_current_terminal = _authenticate(terminal_client)


def _require(principal: Principal, *allowed: str) -> Principal:
    if not principal.roles & set(allowed):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"Requires one of the roles: {', '.join(allowed)}"
        )
    return principal


def require_reader(user: Principal = Depends(_current_user)) -> Principal:
    """Managers and auditors: anything that only reads."""
    return _require(user, MANAGER, AUDITOR)


def require_manager(user: Principal = Depends(_current_user)) -> Principal:
    """Managers only: anything that changes data."""
    return _require(user, MANAGER)


def require_terminal(client: Principal = Depends(_current_terminal)) -> Principal:
    """Card terminals only: the purchase endpoint."""
    return _require(client, TERMINAL)
