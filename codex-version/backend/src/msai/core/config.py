from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RESEARCH_PARALLELISM = max(1, min(4, os.cpu_count() or 1))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False)

    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://msai:msai_dev_password@localhost:5434/msai"
    redis_url: str = "redis://localhost:6381/0"
    data_root: Path = DEFAULT_PROJECT_ROOT / "data"
    strategies_root: Path = DEFAULT_PROJECT_ROOT / "strategies"

    jwt_tenant_id: str = "dev-tenant-id"
    jwt_client_id: str = "dev-client-id"
    msai_api_key: str = ""

    azure_key_vault_url: str | None = None
    polygon_api_key: str | None = None
    databento_api_key: str | None = None
    databento_equities_dataset: str = "EQUS.MINI"
    databento_futures_dataset: str = "GLBX.MDP3"
    databento_default_schema: str = "ohlcv-1m"

    backtest_timeout_seconds: int = 30 * 60
    ingestion_timeout_seconds: int = 60 * 60
    research_timeout_seconds: int = 4 * 60 * 60
    research_max_parallelism: int = DEFAULT_RESEARCH_PARALLELISM
    max_worker_jobs: int = 2
    backtest_max_worker_jobs: int = 1
    ingest_max_worker_jobs: int = 1
    research_worker_jobs: int = 2
    portfolio_max_worker_jobs: int = 1
    queue_retry_attempts: int = 1
    compute_slot_limit: int = max(1, min(8, os.cpu_count() or 1))
    compute_slot_wait_seconds: int = 15 * 60
    compute_slot_poll_seconds: float = 2.0
    compute_slot_lease_seconds: int = 60
    worker_registry_heartbeat_seconds: int = 15
    worker_registry_ttl_seconds: int = 60
    backtest_queue_name: str = "msai:backtest"
    ingest_queue_name: str = "msai:ingest"
    research_queue_name: str = "msai:research"
    portfolio_queue_name: str = "msai:portfolio"
    live_runtime_queue_name: str = "msai:live-runtime"
    live_runtime_request_timeout_seconds: float = 90.0
    queue_allow_abort_jobs: bool = True
    backtest_job_heartbeat_seconds: int = 15
    backtest_job_stale_seconds: int = 10 * 60
    backtest_job_pending_grace_seconds: int = 10 * 60
    research_job_heartbeat_seconds: int = 15
    research_job_stale_seconds: int = 10 * 60
    research_job_pending_grace_seconds: int = 10 * 60
    portfolio_job_heartbeat_seconds: int = 15
    portfolio_job_stale_seconds: int = 10 * 60
    portfolio_job_pending_grace_seconds: int = 10 * 60
    job_watchdog_poll_seconds: int = 60
    optuna_enabled: bool = True
    optuna_max_trials: int = 64
    optuna_min_parallel_batch: int = 2
    daily_ingest_enabled: bool = True
    daily_ingest_timezone: str = "America/Chicago"
    daily_ingest_hour: int = 18
    daily_ingest_minute: int = 30
    daily_ingest_poll_seconds: int = 60

    ib_gateway_host: str = "ib-gateway"
    ib_gateway_port_paper: int = 4002
    ib_gateway_port_live: int = 4001
    ib_data_client_id: int = 11
    ib_exec_client_id: int = 12
    ib_client_id: int = 10
    ib_account_id: str | None = None
    ib_connect_timeout_seconds: float = 4.0
    ib_request_timeout_seconds: int = 60
    ib_allow_mock_fallback: bool = True
    ib_instrument_client_id: int = 13
    live_node_startup_timeout_seconds: float = 30.0
    live_state_snapshot_interval_seconds: float = 5.0

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
    def research_root(self) -> Path:
        return self.data_root / "research"

    @property
    def backtest_analytics_root(self) -> Path:
        return self.reports_root / "analytics"

    @property
    def databento_definition_root(self) -> Path:
        return self.data_root / "databento" / "definitions"

    @property
    def nautilus_catalog_root(self) -> Path:
        return self.data_root / "nautilus"

    @property
    def scheduler_state_path(self) -> Path:
        return self.data_root / "scheduler" / "state.json"

    @property
    def daily_universe_path(self) -> Path:
        return self.data_root / "scheduler" / "daily_universe.json"

    @property
    def alerts_path(self) -> Path:
        return self.data_root / "alerts" / "alerts.json"

    @property
    def graduation_root(self) -> Path:
        return self.research_root / "graduation"

    @property
    def portfolio_root(self) -> Path:
        return self.research_root / "portfolios"

    @property
    def optuna_root(self) -> Path:
        return self.research_root / "optuna"


settings = Settings()
