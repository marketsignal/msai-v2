"""Application configuration loaded from environment variables.

Uses pydantic-settings to load configuration from environment variables and
an optional ``.env`` file. A module-level ``settings`` singleton is provided
for convenient import across the codebase.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# In local dev: .../claude-version/backend/src/msai/core/config.py → parents[4] = claude-version/
# In Docker:    /app/src/msai/core/config.py → parents[3] = /app/
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    """Application-wide settings sourced from environment variables.

    All fields have sensible defaults for local development.  In production,
    every value should be supplied via environment variables or a ``.env``
    file located in the working directory.
    """

    database_url: str = "postgresql+asyncpg://msai:msai_dev_password@localhost:5432/msai"
    redis_url: str = "redis://localhost:6379"
    data_root: Path = _DEFAULT_PROJECT_ROOT / "data"
    strategies_root: Path = _DEFAULT_PROJECT_ROOT / "strategies"
    environment: str = "development"
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    cors_origins: list[str] = ["http://localhost:3000"]
    polygon_api_key: str = ""
    databento_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("data_root", "strategies_root", mode="before")
    @classmethod
    def _cast_path(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        return Path(str(value))


settings: Settings = Settings()
