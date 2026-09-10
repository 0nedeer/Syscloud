"""ASGI middleware also works for streaming responses without buffering their body."""

import logging
from time import perf_counter
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging import request_id_context

logger = logging.getLogger("app.http")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Generate IDs ourselves; never trust arbitrary incoming values in logs/headers.
        request_id = str(uuid4())
        scope["request_id"] = request_id
        token = request_id_context.set(request_id)
        start = perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            # Route template excludes query parameters and caller-provided filenames/IDs.
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
