from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from app.config import Settings


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

    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def empty_reasoning_is_unset(cls, value):
        return None if value == "" else value

    @field_validator("llm_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value):
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

    @field_validator("llm_base_url")
    @classmethod
    def validate_base_url(cls, value):
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

    @model_validator(mode="after")
    def validate_worker(self):
        if self.worker_lease_seconds < 3 * self.worker_heartbeat_seconds:
            raise ValueError("Worker lease must cover at least three heartbeat intervals")
        if not self.llm_api_key.get_secret_value().strip() and urlsplit(
            self.llm_base_url
        ).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("LLM_API_KEY is required for a remote model service")
        return self
