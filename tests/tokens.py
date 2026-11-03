"""Signed test tokens and a stand-in for the identity provider's key lookup.

Tests sign tokens with a throwaway RSA key, so they run without Keycloak and can build
every kind of bad token (expired, wrong issuer, forged signature, ...).
"""

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import get_settings

settings = get_settings()

TEST_KID = "test-signing-key"
MANAGER_USERNAME = "manager.test"


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


PRIVATE_KEY = _new_key()
PUBLIC_KEY = PRIVATE_KEY.public_key()
OTHER_PRIVATE_KEY = _new_key()  # a different key pair, for forged signatures


def public_pem() -> bytes:
    return PUBLIC_KEY.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def make_token(
    roles=("manager",),
    *,
    username: str | None = MANAGER_USERNAME,
    subject: str = "11111111-aaaa-bbbb-cccc-222222222222",
    expires_in: float = 300,
    issuer: str | None = None,
    audience="__default__",
    kid: str | None = TEST_KID,
    key=None,
    algorithm: str = "RS256",
    drop: tuple[str, ...] = (),
    extra: dict | None = None,
) -> str:
    now = int(time.time())
    claims: dict = {
        "iss": issuer or settings.oidc_issuer,
        "aud": settings.oidc_audience if audience == "__default__" else audience,
        "sub": subject,
        "iat": now,
        "exp": now + expires_in,
        "realm_access": {"roles": list(roles)},
    }
    if username is not None:
        claims["preferred_username"] = username
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, key or PRIVATE_KEY, algorithm=algorithm, headers=headers)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unsigned_token(**claims) -> str:
    """A token that claims `alg: none`, i.e. no signature at all."""
    header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": TEST_KID}).encode())
    payload = _b64(json.dumps(_default_claims() | claims).encode())
    return f"{header}.{payload}."


def hmac_token_signed_with_public_key() -> str:
    """The classic algorithm-confusion attack: HS256, using the public key as the shared secret."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": TEST_KID}).encode())
    payload = _b64(json.dumps(_default_claims()).encode())
    signature = hmac.new(public_pem(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def _default_claims() -> dict:
    now = int(time.time())
    return {
        "iss": settings.oidc_issuer,
        "aud": settings.oidc_audience,
        "sub": "attacker",
        "exp": now + 300,
        "realm_access": {"roles": ["manager"]},
    }


class FakeJwksClient:
    """Knows one signing key. Mirrors the parts of PyJWKClient the API uses."""

    def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != TEST_KID:
            raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid!r}")
        return SimpleNamespace(key=PUBLIC_KEY)


class UnreachableJwksClient:
    def get_signing_key_from_jwt(self, token: str):
        raise jwt.PyJWKClientConnectionError("Fail to fetch data from the url, err: connection refused")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
