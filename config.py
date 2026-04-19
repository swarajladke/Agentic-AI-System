"""Global application settings."""

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Agentic AI System"
    environment: str = "development"
    redis_url: str = "redis://localhost:6379/0"

    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-20250514", alias="ANTHROPIC_MODEL")
    anthropic_api_url: str = "https://api.anthropic.com/v1/messages"
    anthropic_version: str = "2023-06-01"

    batch_size: int = Field(default=5, alias="BATCH_SIZE")
    batch_window_seconds: float = Field(default=2.0, alias="BATCH_WINDOW_SECONDS")
    stream_block_ms: int = Field(default=3000, alias="STREAM_BLOCK_MS")
    claim_idle_ms: int = Field(default=30000, alias="CLAIM_IDLE_MS")

    api_timeout_seconds: float = 45.0
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0

    retriever_use_real_search: bool = False
    brave_api_key: Optional[str] = None

    task_stream: str = "stream:tasks"
    result_stream: str = "stream:results"
    dlq_stream: str = "stream:dlq"

    def user_stream_name(self, task_id: str) -> str:
        """Return task-specific user output stream key."""
        return f"stream:user_output:{task_id}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return singleton settings."""
    return Settings()
