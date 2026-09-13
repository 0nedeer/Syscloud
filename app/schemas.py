"""HTTP 响应模型与 LLM 摘要的严格数据契约。"""

from datetime import UTC, datetime
from typing import Annotated, overload
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer

from app.models import TaskStatus


@overload
def iso_utc(value: datetime) -> str: ...


@overload
def iso_utc(value: None) -> None: ...


# 数据库返回的无时区 datetime 按 UTC 解释，统一输出带 Z 的 ISO 8601 字符串。
def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# 允许从对象属性读值，并拒绝未声明字段，尽早暴露响应组装错误。
class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


# 对外错误只含稳定 code 与可读 message，不传供应商原始报错。
class TaskError(ApiModel):
    code: str
    message: str


class ApiErrorDetail(TaskError):
    """HTTP 错误携带请求 ID，便于关联脱敏日志。"""

    request_id: str | None = None


class ErrorResponse(ApiModel):
    error: ApiErrorDetail


# 三个字段严格校验：非空摘要字符串、要点字符串列表、待办字符串列表。
# strict=True 防止数字等被悄悄转成字符串；extra=forbid 拒绝模型添加额外字段。
class SummaryResult(ApiModel):
    """API 输出与模型结果共用同一份严格摘要契约。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    key_points: list[str]
    todos: list[str]


# 租约令牌、磁盘路径等内部调度细节不进入此响应。
class TaskResponse(ApiModel):
    task_id: UUID
    recording_id: UUID
    status: TaskStatus
    attempt_no: int
    retry_of_task_id: UUID | None = None
    transcript: str | None = None
    summary_result: SummaryResult | None = None
    error: TaskError | None = None
    auto_retry_count: int = Field(default=0, ge=0, le=3)
    max_auto_retries: int = 3
    retry_waiting: bool = False
    next_attempt_at: datetime | None = None
    last_error: TaskError | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_serializer("created_at", "started_at", "finished_at", "next_attempt_at")
    def serialize_datetime(self, value: datetime | None) -> str | None:
        return iso_utc(value)


class RecordingCreateResponse(ApiModel):
    recording_id: UUID
    task_id: UUID
    status: TaskStatus = TaskStatus.PENDING


class RecordingListTask(ApiModel):
    task_id: UUID
    status: TaskStatus
    attempt_no: int


class RecordingListItem(ApiModel):
    recording_id: UUID
    original_filename: str
    size_bytes: int
    created_at: datetime
    latest_task: RecordingListTask | None

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return iso_utc(value)


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
    summary_result: SummaryResult | None = None

    @field_serializer("created_at")
    def serialize_created_at(self, value: datetime) -> str:
        return iso_utc(value)


class RetryResponse(ApiModel):
    recording_id: UUID
    task_id: UUID
    status: TaskStatus
    attempt_no: int
