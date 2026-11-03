import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


AUTH_ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing, expired or invalid access token"},
    403: {"model": ErrorResponse, "description": "The token's role does not allow this action"},
    503: {"model": ErrorResponse, "description": "Identity provider not available"},
}
NOT_FOUND = {404: {"model": ErrorResponse, "description": "Not found"}}
CONFLICT = {409: {"model": ErrorResponse, "description": "Conflict with existing data"}}
INVALID_STATE = {
    422: {"model": ErrorResponse, "description": "Validation error or invalid state"}
}


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, details: Any | None = None):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InvalidStateError(AppError):
    status_code = 422
    code = "invalid_state"


def _response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
):
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError):
        return _response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException):
        # Keep headers such as WWW-Authenticate on 401 responses.
        return _response(exc.status_code, "http_error", str(exc.detail), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError):
        return _response(
            422, "validation_error", "Request validation failed", jsonable(exc.errors())
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _response(500, "internal_error", "Internal server error")


def jsonable(errors: list[dict]) -> list[dict]:
    # Pydantic puts raw exception objects in ctx, which JSON cannot encode.
    return [
        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in errors
    ]
