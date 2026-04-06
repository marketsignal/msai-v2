"""Application configuration loaded from environment variables.

Uses pydantic-settings to load configuration from environment variables and
an optional ``.env`` file. A module-level ``settings`` singleton is provided
for convenient import across the codebase.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings sourced from environment variables.

    All fields have sensible defaults for local development.  In production,
    every value should be supplied via environment variables or a ``.env``
    file located in the working directory.
    """

    database_url: str = "postgresql+asyncpg://msai:msai_dev_password@localhost:5432/msai"
    redis_url: str = "redis://localhost:6379"
    data_root: str = "./data"
    environment: str = "development"
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    cors_origins: list[str] = ["http://localhost:3000"]
    polygon_api_key: str = ""
    databento_api_key: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings: Settings = Settings()
