import logging

from fastapi import APIRouter, Request
from sqlalchemy.exc import SQLAlchemyError

from app.db import SchemaNotReady
from app.errors import ApiError

router = APIRouter(prefix="/health", tags=["health"])
logger = logging.getLogger("app.health")


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/ready", responses={503: {"description": "Database or schema is not ready"}})
async def ready(request: Request) -> dict[str, str]:
    try:
        await request.app.state.database.check_ready(
            request.app.state.settings.readiness_timeout_seconds
        )
    except SchemaNotReady:
        raise ApiError(503, "schema_not_ready", "Database migrations are not up to date.") from None
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        logger.warning("readiness_failed", extra={"exception_type": type(exc).__name__})
        raise ApiError(503, "database_unavailable", "Database is unavailable.") from None
    return {"status": "ready"}
