"""公共配置与 Worker 配置，在启动时校验环境变量。"""

from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


# 环境变量通常优先于 .env；额外变量忽略，便于本地与 Compose 共用模板。
# SecretStr 隐藏默认显示值，hide_input_in_errors 避免配置错误带出连接串。
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    database_url: SecretStr
    upload_dir: Path = Path("data/recordings")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_read_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_pool_timeout_seconds: int = Field(default=5, ge=1, le=60)
    readiness_timeout_seconds: float = Field(default=3, gt=0, le=60)

    # 只接受 mysql+asyncmy 的明确库连接；参数限定 charset=utf8mb4。
    # 这里验证格式，不进行实际连接，数据库可达性由 Database.check_ready 检查。
    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            valid = (
                url.drivername == "mysql+asyncmy"
                and bool(url.host)
                and bool(url.username)
                and bool(url.database)
                and url.port != 0
            )
        except (ArgumentError, ValueError):
            valid = False
        if not valid:
            raise ValueError("DATABASE_URL must name a MySQL database using mysql+asyncmy")
        # The application always establishes utf8mb4 and UTC sessions.
        if url.query and url.query != {"charset": "utf8mb4"}:
            raise ValueError("Only charset=utf8mb4 is supported in DATABASE_URL query options")
        return value


# 并发 1～3、租约覆盖至少三个心跳间隔；远程模型地址和代理分别校验。


class WorkerSettings(Settings):
    llm_base_url: str = Field(repr=False)
    llm_model: str = Field(min_length=1)
    llm_api_key: SecretStr = SecretStr("")
    llm_api_style: Literal["responses", "chat_completions"] = "responses"
    llm_reasoning_effort: Literal["low", "medium", "high"] | None = None
    llm_proxy_url: SecretStr = SecretStr("")
    llm_timeout_seconds: float = Field(default=60, gt=0, le=300)
    llm_connect_timeout_seconds: float = Field(default=5, gt=0, le=60)
    llm_max_response_bytes: int = Field(default=262144, ge=1024, le=1048576)
    worker_concurrency: int = Field(default=3, ge=1, le=3)
    worker_poll_seconds: float = Field(default=1, ge=0.05, le=60)
    worker_lease_seconds: int = Field(default=120, ge=3, le=3600)
    worker_heartbeat_seconds: float = Field(default=10, ge=0.1, le=300)
    worker_shutdown_seconds: float = Field(default=10, ge=0, le=60)
    worker_cleanup_seconds: float = Field(default=10, ge=0.1, le=300)
    worker_retry_base_seconds: float = Field(default=2, gt=0, le=60)

    # 环境文件中的空字符串表示不发送 reasoning 参数，兼容不支持该参数的模型。
    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def empty_reasoning_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    # 代理可含认证信息但整体由 SecretStr 隐藏；限制为 HTTP(S) 代理地址。
    @field_validator("llm_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw:
            return value
        try:
            url = urlsplit(raw)
            valid = (
                url.scheme in {"http", "https"}
                and bool(url.hostname)
                and url.port != 0
                and not url.query
                and not url.fragment
                and url.path in {"", "/"}
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("LLM_PROXY_URL must be an HTTP(S) proxy address")
        return value

    # 远程服务要求 HTTPS，仅回环地址允许 HTTP；禁止把凭据放进基础 URL。
    @field_validator("llm_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            local = url.hostname in {"localhost", "127.0.0.1", "::1"}
            valid = (
                bool(url.hostname)
                and (url.scheme == "https" or (local and url.scheme == "http"))
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and url.port != 0
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("LLM_BASE_URL requires HTTPS (or loopback HTTP), without credentials")
        return value.rstrip("/")

    # 执行跨字段检查：心跳与租约比例合理、远程密钥非空。
    @model_validator(mode="after")
    def validate_worker(self) -> Self:
        if self.worker_lease_seconds < 3 * self.worker_heartbeat_seconds:
            raise ValueError("Worker lease must cover at least three heartbeat intervals")
        if not self.llm_api_key.get_secret_value().strip() and urlsplit(
            self.llm_base_url
        ).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("LLM_API_KEY is required for a remote model service")
        return self
