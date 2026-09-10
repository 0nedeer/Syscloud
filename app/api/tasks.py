from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import TaskResponse
from app.services.queries import get_task

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskResponse)
async def task_detail(task_id: UUID, session: Annotated[AsyncSession, Depends(get_session)]):
    return await get_task(session, str(task_id))
