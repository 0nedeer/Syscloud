"""统一业务、参数、数据库及框架异常的公开错误响应。"""

import logging
from collections.abc import Mapping
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as DatabaseTimeout
from starlette.exceptions import HTTPException

logger = logging.getLogger("app.errors")
UNAVAILABLE_DATABASE_CODES = frozenset(
    {1040, 1042, 1043, 1045, 1049, 1205, 1213, 2002, 2003, 2006, 2013}
)


# 业务可主动指定 HTTP 状态、稳定业务错误码和可公开消息。
class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# 所有错误使用同一 JSON 外壳，并保留 Allow 等必要 HTTP 响应头。
def error_response(
    request: Request,
    status: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = request.scope.get("request_id")
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
        headers={**(headers or {}), "X-Request-ID": request_id or ""},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(request, exc.status_code, exc.code, exc.message)

    # 把 FastAPI 参数校验失败统一为 400，不直接返回可能含用户输入的详细校验内容。
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(request, 400, "invalid_request", "Request validation failed.")

    # 处理框架级 404/405 等，使用标准状态描述，保留原始协议响应头。
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        try:
            message = HTTPStatus(exc.status_code).phrase
        except ValueError:
            message = "HTTP request failed."
        return error_response(request, exc.status_code, code, message, exc.headers)

    # 连接、超时、死锁等已知可用性问题返回 503；未知数据库错误按内部错误 500 处理。
    @app.exception_handler(OperationalError)
    @app.exception_handler(DatabaseTimeout)
    async def database_error(
        request: Request, exc: OperationalError | DatabaseTimeout
    ) -> JSONResponse:
        # 驱动异常不一定携带错误号；未知形态按内部错误处理，避免错误处理器再次报错。
        if isinstance(exc, OperationalError) and (
            not exc.orig.args or exc.orig.args[0] not in UNAVAILABLE_DATABASE_CODES
        ):
            logger.error("database_error", extra={"exception_type": type(exc).__name__})
            return error_response(request, 500, "internal_error", "An internal error occurred.")
        logger.error("database_unavailable", extra={"exception_type": type(exc).__name__})
        return error_response(request, 503, "database_unavailable", "Database is unavailable.")

    # 最后兜底：只记录异常类型和请求 ID，客户端收到通用 500。
    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "request_failed",
            extra={
                "exception_type": type(exc).__name__,
                "request_id": request.scope.get("request_id"),
            },
        )
        return error_response(request, 500, "internal_error", "An internal error occurred.")
