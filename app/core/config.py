from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"
    log_format: str = "text"

    database_url_async: str
    database_url_sync: str
    redis_url: str

    manager_api_key: str = Field(min_length=8)
    terminal_api_key: str = Field(min_length=8)

    frequency_max_purchases: int = 3
    frequency_window_minutes: int = 30
    large_purchase_multiplier: float = 2.0
    large_purchase_min_history: int = 5
    large_purchase_fallback_tank_ratio: float = 0.8

    analysis_max_retries: int = 5
    stuck_after_minutes: int = 5
    recovery_interval_seconds: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
