from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas import RecordingCreateResponse, RecordingDetailResponse, RecordingListResponse
from app.services.deletions import delete_recording
from app.services.queries import get_recording, list_recordings
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


@router.get("", response_model=RecordingListResponse)
async def recordings_list(
    session: Annotated[AsyncSession, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    return await list_recordings(session, page, page_size)


@router.delete("/{recording_id}", status_code=204)
async def recording_delete(
    recording_id: UUID,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await delete_recording(session, request.app.state.storage, str(recording_id))
