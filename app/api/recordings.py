from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile

from app.schemas import RecordingCreateResponse
from app.services.uploads import create_recording

router = APIRouter(prefix="/v1/recordings", tags=["recordings"])


@router.post("", status_code=202, response_model=RecordingCreateResponse)
async def upload_recording(request: Request, file: Annotated[UploadFile, File()]):
    return await create_recording(request.app.state.database, request.app.state.storage, file)
