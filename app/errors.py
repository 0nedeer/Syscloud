"""One public error envelope for domain, validation, HTTP and unexpected failures."""

import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as DatabaseTimeout
from starlette.exceptions import HTTPException

logger = logging.getLogger("app.errors")


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(request: Request, status: int, code: str, message: str, headers=None):
    request_id = request.scope.get("request_id")
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers={**(headers or {}), "X-Request-ID": request_id or ""},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(request, 400, "invalid_request", "Request validation failed.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        try:
            message = HTTPStatus(exc.status_code).phrase
        except ValueError:
            message = "HTTP request failed."
        return error_response(request, exc.status_code, code, message, exc.headers)

    @app.exception_handler(OperationalError)
    @app.exception_handler(DatabaseTimeout)
    async def database_error(request: Request, exc: Exception):
        if isinstance(exc, OperationalError) and exc.orig.args[0] not in {
            1040,
            1042,
            1043,
            1045,
            1049,
            1205,
            1213,
            2002,
            2003,
            2006,
            2013,
        }:
            logger.error("database_error", extra={"exception_type": type(exc).__name__})
            return error_response(request, 500, "internal_error", "An internal error occurred.")
        logger.error("database_unavailable", extra={"exception_type": type(exc).__name__})
        return error_response(request, 503, "database_unavailable", "Database is unavailable.")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error(
            "request_failed",
            extra={
                "exception_type": type(exc).__name__,
                "request_id": request.scope.get("request_id"),
            },
        )
        return error_response(request, 500, "internal_error", "An internal error occurred.")
