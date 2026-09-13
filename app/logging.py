"""按请求和任务 ID 关联的结构化日志与 ASGI 中间件。"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# 结构化日志：按 request_id / recording_id / task_id 关联一次请求与后续任务。
# ContextVar 隔离并发请求的上下文；字段白名单避免随意记录用户数据。


request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
FIELDS = (
    "request_id",
    "recording_id",
    "task_id",
    "storage_key",
    "previous_status",
    "new_status",
    "duration_ms",
    "error_code",
    "auto_retry_count",
    "next_attempt_at",
    "exception_type",
    "method",
    "route",
    "status_code",
)


# 每行输出一个 JSON 对象，可被日志工具检索；extra 中仅允许白名单字段。
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_context.get()),
            "recording_id": getattr(record, "recording_id", None),
            "task_id": getattr(record, "task_id", None),
        }
        data.update({key: getattr(record, key) for key in FIELDS if hasattr(record, key)})
        # 不序列化异常消息和堆栈；驱动错误可能包含凭据或 SQL。
        if record.exc_info and record.exc_info[0]:
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False)


# 同时约束 Uvicorn 异常日志，压低数据库及 HTTP 客户端的详细日志级别。
def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    # Uvicorn 会再次记录未处理的 ASGI 异常；同样不把堆栈输出到公开日志。
    server_logger = logging.getLogger("uvicorn.error")
    server_logger.handlers.clear()
    server_logger.addHandler(handler)
    server_logger.propagate = False
    for name in ("sqlalchemy.engine", "httpx", "httpcore", "asyncmy"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


# 原生 ASGI 请求中间件：为 HTTP 请求生成 ID，透传响应并记录耗时。


logger = logging.getLogger("app.http")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    # 生成可信 UUID，不采用用户输入作为日志 ID；finally 恢复 ContextVar 防止串请求。
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope["request_id"] = request_id
        token = request_id_context.set(request_id)
        start = perf_counter()
        status = 500

        # 只拦截响应起始消息添加 X-Request-ID，后续 body 逐块透传，不拼接流。
        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            # 路由模板排除查询参数以及调用方提供的文件名和 ID。
            route = scope.get("route")
            logger.info(
                "http_request",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "route": getattr(route, "path", "unmatched"),
                    "status_code": status,
                    "duration_ms": round((perf_counter() - start) * 1000, 2),
                },
            )
            request_id_context.reset(token)
