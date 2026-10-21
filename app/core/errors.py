"""Domain errors plus the handlers that turn every failure into one JSON envelope:

    {"error": {"code": "...", "message": "...", "details": ...}}
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class InsufficientStockError(ConflictError):
    code = "insufficient_stock"


class InvalidRequestError(AppError):
    """Request is well-formed but violates a business rule (e.g. date_from after date_to)."""

    status_code = 422
    code = "invalid_request"


class AIUnavailableError(AppError):
    """The AI model is not configured or its provider failed."""

    status_code = 503
    code = "ai_unavailable"


def _envelope(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return _envelope(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(part) for part in err["loc"] if part != "body"), "message": err["msg"]}
            for err in exc.errors()
        ]
        return _envelope(422, "validation_error", "Request validation failed", details)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return _envelope(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", exc_info=exc)
        return _envelope(500, "internal_error", "Internal server error")
