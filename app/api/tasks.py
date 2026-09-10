from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import RetryResponse, TaskResponse
from app.services.queries import get_task
from app.services.retries import retry_task

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskResponse)
async def task_detail(task_id: UUID, session: Annotated[AsyncSession, Depends(get_session)]):
    return await get_task(session, str(task_id))


@router.post(
    "/{task_id}/retry",
    status_code=202,
    response_model=RetryResponse,
    responses={200: {"model": RetryResponse, "description": "Existing successor task"}},
)
async def retry(
    task_id: UUID, response: Response, session: Annotated[AsyncSession, Depends(get_session)]
):
    result, created = await retry_task(session, str(task_id))
    response.status_code = 202 if created else 200
    return result
