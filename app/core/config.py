from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"
    log_format: str = "text"

    database_url_async: str
    database_url_sync: str
    redis_url: str

    # OAuth2 / OpenID Connect. The issuer must match the "iss" claim of the tokens exactly;
    # it is the address callers (browsers, terminals) use to reach the identity provider.
    oidc_issuer: str = Field(min_length=8)
    oidc_audience: str = "fuelguard-api"
    # Where the API itself fetches the signing keys. Differs from the issuer when the API runs
    # in a container and reaches the provider by its service name. Defaults to the issuer.
    oidc_jwks_url: str | None = None

    frequency_max_purchases: int = 3
    frequency_window_minutes: int = 30
    large_purchase_multiplier: float = 2.0
    large_purchase_min_history: int = 5
    large_purchase_fallback_tank_ratio: float = 0.8

    analysis_max_retries: int = 5
    stuck_after_minutes: int = 5
    recovery_interval_seconds: int = 120

    @field_validator("oidc_issuer")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer}/protocol/openid-connect/certs"

    @property
    def oidc_authorization_url(self) -> str:
        return f"{self.oidc_issuer}/protocol/openid-connect/auth"

    @property
    def oidc_token_url(self) -> str:
        return f"{self.oidc_issuer}/protocol/openid-connect/token"


@lru_cache
def get_settings() -> Settings:
    return Settings()
