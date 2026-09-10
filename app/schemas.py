"""Public response models shared by recording and task endpoints."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.models import TaskStatus


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class TaskError(ApiModel):
    code: str
    message: str


class TaskResponse(ApiModel):
    task_id: UUID
    recording_id: UUID
    status: TaskStatus
    attempt_no: int
    retry_of_task_id: UUID | None = None
    transcript: str | None = None
    summary_result: dict[str, Any] | None = None
    error: TaskError | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_serializer("created_at", "started_at", "finished_at")
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return iso_utc(value)


class RecordingCreateResponse(ApiModel):
    recording_id: UUID
    task_id: UUID
    status: str = "pending"


class RecordingListTask(ApiModel):
    task_id: UUID
    status: str
    attempt_no: int


class RecordingListItem(ApiModel):
    recording_id: UUID
    original_filename: str
    size_bytes: int
    created_at: datetime
    latest_task: RecordingListTask | None

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return iso_utc(value)  # type: ignore[return-value]


class RecordingListResponse(ApiModel):
    items: list[RecordingListItem]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)


class RecordingDetailResponse(ApiModel):
    recording_id: UUID
    original_filename: str
    size_bytes: int
    created_at: datetime
    latest_task: TaskResponse | None
    transcript: str | None = None
    summary_result: dict[str, Any] | None = None

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return iso_utc(value)  # type: ignore[return-value]


class RetryResponse(ApiModel):
    recording_id: UUID
    task_id: UUID
    status: str
    attempt_no: int
