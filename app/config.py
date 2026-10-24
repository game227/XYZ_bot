from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "dev"
    log_level: str = "INFO"
    sentry_dsn: SecretStr | None = None  # optional error tracking (pip install ".[monitoring]")

    # Telegram
    bot_token: SecretStr
    superadmin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # Storage
    database_url: str = "postgresql+asyncpg://xyz:xyz@localhost:5432/xyz"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    redis_url: str = "redis://localhost:6379/0"

    # Materials sent straight to the bot (stored as Telegram file_id, never downloaded)
    telegram_document_max_mb: int = 200
    telegram_video_max_mb: int = 2000

    # Notifications / live sessions
    broadcast_rate_per_second: int = 25
    live_reminder_minutes: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [1440, 60])

    # Business rules
    timezone: str = "Asia/Tashkent"
    free_lesson_limit: int = 2
    teacher_session_ttl_hours: int = 24

    @field_validator("superadmin_ids", "live_reminder_minutes", mode="before")
    @classmethod
    def _split_ints(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(" ", "").split(",") if part]
        if isinstance(value, int):
            return [value]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
