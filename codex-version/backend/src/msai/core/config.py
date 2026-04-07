from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False)

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://msai:msai_dev_password@localhost:5432/msai"
    redis_url: str = "redis://localhost:6379/0"
    data_root: Path = DEFAULT_PROJECT_ROOT / "data"
    strategies_root: Path = DEFAULT_PROJECT_ROOT / "strategies"

    jwt_tenant_id: str = "dev-tenant-id"
    jwt_client_id: str = "dev-client-id"
    msai_api_key: str = ""

    azure_key_vault_url: str | None = None
    polygon_api_key: str | None = None
    databento_api_key: str | None = None

    backtest_timeout_seconds: int = 30 * 60
    ingestion_timeout_seconds: int = 60 * 60
    max_worker_jobs: int = 2
    queue_retry_attempts: int = 1

    ib_gateway_host: str = "ib-gateway"
    ib_gateway_port_paper: int = 4002
    ib_gateway_port_live: int = 4001
    ib_data_client_id: int = 11
    ib_exec_client_id: int = 12
    ib_client_id: int = 10
    ib_account_id: str | None = None
    ib_connect_timeout_seconds: float = 4.0
    ib_allow_mock_fallback: bool = True

    nautilus_trader_id: str = "TRADER-001"

    @field_validator("data_root", "strategies_root", mode="before")
    @classmethod
    def _cast_path(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        return Path(str(value))

    @property
    def parquet_root(self) -> Path:
        return self.data_root / "parquet"

    @property
    def reports_root(self) -> Path:
        return self.data_root / "reports"

    @property
    def nautilus_catalog_root(self) -> Path:
        return self.data_root / "nautilus"


settings = Settings()
