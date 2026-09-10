"""Small structured logger with an explicit allowlist of non-sensitive fields."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

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
        # Do not serialize exception messages/tracebacks; driver errors can contain credentials/SQL.
        if record.exc_info and record.exc_info[0]:
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    # Uvicorn re-logs unhandled ASGI exceptions; keep its tracebacks out of public logs too.
    server_logger = logging.getLogger("uvicorn.error")
    server_logger.handlers.clear()
    server_logger.addHandler(handler)
    server_logger.propagate = False
    for name in ("sqlalchemy.engine", "httpx", "httpcore", "asyncmy"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
