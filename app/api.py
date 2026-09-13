"""健康、录音与任务 HTTP 接口；事务规则由业务服务处理。"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SchemaNotReady, get_session
from app.errors import ApiError
from app.schemas import (
    ErrorResponse,
    RecordingCreateResponse,
    RecordingDetailResponse,
    RecordingListResponse,
    RetryResponse,
    TaskResponse,
)
from app.services.deletions import delete_recording
from app.services.queries import get_recording, get_task, list_recordings
from app.services.retries import retry_task
from app.services.uploads import create_recording

# 4XX 声明统一客户端错误，同时避免框架自动生成与实际响应不符的 422 契约。
router = APIRouter(
    responses={
        "4XX": {"model": ErrorResponse, "description": "Client request error"},
        500: {"model": ErrorResponse, "description": "Unexpected internal error"},
        503: {"model": ErrorResponse, "description": "Dependency unavailable"},
    }
)
logger = logging.getLogger("app.health")


# 不访问数据库，数据库故障时仍可用来区分 API 进程是否存活。
@router.get("/health/live", tags=["health"], summary="检查进程存活")
async def live() -> dict[str, str]:
    return {"status": "alive"}


# 有界超时检查数据库；迁移不匹配和数据库不可用都返回 503，但错误码不同。
@router.get(
    "/health/ready",
    tags=["health"],
    summary="检查数据库与迁移就绪",
    responses={503: {"model": ErrorResponse, "description": "Database or schema is not ready"}},
)
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




# 新建返回 202 表示已接收异步工作；去重命中复用已有资源，避免重复排队。
@router.post(
    "/v1/recordings",
    tags=["recordings"],
    status_code=202,
    response_model=RecordingCreateResponse,
    summary="上传录音并创建处理任务",
    responses={
        200: {"model": RecordingCreateResponse, "description": "Duplicate audio"},
        400: {"model": ErrorResponse, "description": "Missing, empty or unsupported audio"},
        409: {"model": ErrorResponse, "description": "Concurrent upload or deletion conflict"},
        413: {"model": ErrorResponse, "description": "Audio exceeds 50 MiB"},
    },
)
async def upload_recording(
    request: Request, response: Response, file: Annotated[UploadFile, File()]
) -> RecordingCreateResponse:
    result, created = await create_recording(
        request.app.state.database, request.app.state.storage, file
    )
    response.status_code = 202 if created else 200
    return result


@router.get(
    "/v1/recordings/{recording_id}",
    tags=["recordings"],
    response_model=RecordingDetailResponse,
    summary="查询录音详情及最新处理结果",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid recording ID"},
        404: {"model": ErrorResponse, "description": "Recording not found"},
    },
)
async def recording_detail(
    recording_id: UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> RecordingDetailResponse:
    return await get_recording(session, str(recording_id))


@router.get(
    "/v1/recordings",
    tags=["recordings"],
    response_model=RecordingListResponse,
    summary="按创建时间倒序分页查询录音",
    responses={400: {"model": ErrorResponse, "description": "Invalid pagination parameters"}},
)
async def recordings_list(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RecordingListResponse:
    return await list_recordings(session, page, page_size)


@router.delete(
    "/v1/recordings/{recording_id}",
    tags=["recordings"],
    status_code=204,
    summary="删除录音文件与关联任务",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid recording ID"},
        404: {"model": ErrorResponse, "description": "Recording not found"},
    },
)
async def recording_delete(
    recording_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await delete_recording(session, request.app.state.storage, str(recording_id))


@router.get(
    "/v1/tasks/{task_id}",
    tags=["tasks"],
    response_model=TaskResponse,
    summary="查询任务状态、结果与失败原因",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid task ID"},
        404: {"model": ErrorResponse, "description": "Task not found"},
    },
)
async def task_detail(
    task_id: UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> TaskResponse:
    return await get_task(session, str(task_id))


# 重试创建后继任务保持幂等，避免产生分叉历史。
@router.post(
    "/v1/tasks/{task_id}/retry",
    tags=["tasks"],
    status_code=202,
    response_model=RetryResponse,
    summary="为失败任务创建或取得后继任务",
    responses={
        200: {"model": RetryResponse, "description": "Existing successor task"},
        400: {"model": ErrorResponse, "description": "Invalid task ID"},
        404: {"model": ErrorResponse, "description": "Task not found"},
        409: {"model": ErrorResponse, "description": "Only failed tasks can be retried"},
    },
)
async def retry(
    task_id: UUID, response: Response, session: Annotated[AsyncSession, Depends(get_session)]
) -> RetryResponse:
    result, created = await retry_task(session, str(task_id))
    response.status_code = 202 if created else 200
    return result
