"""Validated settings; secrets must not appear in startup failures or logs."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


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
