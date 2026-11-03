import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from app.core.config import get_settings

manager_key_header = APIKeyHeader(
    name="X-Manager-Key", scheme_name="ManagerKey", auto_error=False
)
terminal_key_header = APIKeyHeader(
    name="X-Terminal-Key", scheme_name="TerminalKey", auto_error=False
)


def _check(provided: str | None, expected: str) -> None:
    if not provided:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid API key")


def require_manager(key: str | None = Security(manager_key_header)) -> None:
    _check(key, get_settings().manager_api_key)


def require_terminal(key: str | None = Security(terminal_key_header)) -> None:
    _check(key, get_settings().terminal_api_key)
