from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import RecordingCreateResponse, RecordingDetailResponse
from app.services.queries import get_recording
from app.services.uploads import create_recording

router = APIRouter(prefix="/v1/recordings", tags=["recordings"])


@router.post("", status_code=202, response_model=RecordingCreateResponse)
async def upload_recording(request: Request, file: Annotated[UploadFile, File()]):
    return await create_recording(request.app.state.database, request.app.state.storage, file)


@router.get("/{recording_id}", response_model=RecordingDetailResponse)
async def recording_detail(
    recording_id: UUID, session: Annotated[AsyncSession, Depends(get_session)]
):
    return await get_recording(session, str(recording_id))
