"""Application configuration loaded from environment variables.

Uses pydantic-settings to load configuration from environment variables and
an optional ``.env`` file. A module-level ``settings`` singleton is provided
for convenient import across the codebase.
"""

from __future__ import annotations

from os import cpu_count
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
    msai_api_key: str = ""
    cors_origins: list[str] = ["http://localhost:3000"]
    polygon_api_key: str = ""
    databento_api_key: str = ""
    databento_equities_dataset: str = "EQUS.MINI"
    databento_futures_dataset: str = "GLBX.MDP3"
    databento_default_schema: str = "ohlcv-1m"

    # Interactive Brokers account id for live deployments. Part of the
    # stable identity tuple (decision #7) — switching accounts produces
    # a new ``identity_signature`` and therefore a cold-start deployment
    # with isolated state. Paper accounts start with ``DU``, live with ``U``.
    ib_account_id: str = "DU0000000"

    # Interactive Brokers Gateway connection (Phase 4 task #154
    # scope-B). Host + port together must match the account type:
    # paper accounts (``DU...``) with port 4002, live accounts
    # (``U...``) with port 4001. The ``live_node_config.py`` builder
    # validates this via ``_validate_port_account_consistency`` —
    # a mismatch crashes the subprocess at ``build_live_trading_node_config``
    # time rather than silently sending orders to the wrong venue.
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002

    # IB market data type: REALTIME (default, requires subscription),
    # DELAYED (15-min delayed, free for most instruments),
    # DELAYED_FROZEN (last available delayed snapshot).
    # Set via IB_MARKET_DATA_TYPE env var. If your paper account
    # doesn't have real-time US equity data, set to DELAYED.
    ib_market_data_type: str = "REALTIME"

    # Whether to restrict bar data to regular trading hours only.
    # False = include extended/after-hours data (required for FX 24h
    # and for equity strategies that trade pre/post market).
    # True = RTH bars only (default for equity day-trading).
    ib_use_regular_trading_hours: bool = False

    # Maximum wait for ``trader.is_running`` to flip True after
    # ``node.run_async`` starts (Phase 1 task 1.8 / decision #14).
    # Exceeding this marks the row ``failed`` /
    # ``FailureKind.RECONCILIATION_FAILED``.
    startup_health_timeout_s: float = 60.0

    # Backtest execution tuning
    backtest_timeout_seconds: int = 30 * 60

    # Portfolio job wall-clock budget.  Portfolio runs launch N candidate
    # backtests, each bounded by ``backtest_timeout_seconds``; the arq
    # ``job_timeout`` has to cover the *sequential* worst case —
    # ``ceil(N / parallelism) × backtest_timeout`` with headroom — or
    # otherwise valid portfolios get killed before any child actually
    # times out.  Sizing:
    #
    #     portfolio_job_timeout_seconds ≈ ceil(max_N / compute_slot_limit)
    #                                     × backtest_timeout_seconds × 1.1
    #
    # Defaults to ~8 sequential 30-min batches (4 h).  Operators who raise
    # ``backtest_timeout_seconds`` or expect portfolios with >8 allocations
    # AND low ``max_parallelism`` must bump this in lockstep.
    portfolio_job_timeout_seconds: int = 8 * 30 * 60  # 4 hours

    # Job watchdog thresholds
    job_stale_seconds: int = 600  # 10 min without heartbeat = stale
    job_pending_grace_seconds: int = 600  # 10 min pending without starting = stuck

    # Queue names (dedicated queues prevent cross-worker job leakage)
    research_queue_name: str = "msai:research"
    portfolio_queue_name: str = "msai:portfolio"
    research_worker_jobs: int = 2
    research_timeout_seconds: int = 14400  # 4 hours
    research_max_parallelism: int = max(1, min(4, (cpu_count() or 1) - 1))
    optuna_enabled: bool = True
    optuna_max_trials: int = 64

    # Compute slot management (Redis semaphore for concurrent job limits)
    compute_slot_limit: int = 4
    compute_slot_wait_seconds: int = 900  # max time to wait for a slot
    compute_slot_lease_seconds: int = 120  # TTL per lease
    compute_slot_poll_seconds: int = 2  # polling interval while waiting

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("data_root", "strategies_root", mode="before")
    @classmethod
    def _cast_path(cls, value: object) -> Path:
        if isinstance(value, Path):
            return value
        return Path(str(value))

    @property
    def parquet_root(self) -> Path:
        """Root directory for raw OHLCV Parquet files (``{data_root}/parquet``).

        Organized by ``{asset_class}/{symbol}/{YYYY}/{MM}.parquet`` and written
        by the data ingestion pipeline.
        """
        return self.data_root / "parquet"

    @property
    def reports_root(self) -> Path:
        """Root directory for generated QuantStats HTML reports
        (``{data_root}/reports``).
        """
        return self.data_root / "reports"

    @property
    def research_root(self) -> Path:
        """Root directory for research engine output (``{data_root}/research``)."""
        return self.data_root / "research"

    @property
    def optuna_root(self) -> Path:
        """Root directory for Optuna study journals (``{research_root}/optuna``)."""
        return self.research_root / "optuna"

    @property
    def nautilus_catalog_root(self) -> Path:
        """Root directory for the NautilusTrader ``ParquetDataCatalog``
        (``{data_root}/nautilus``).

        The catalog is lazily built from raw Parquet files on the first
        backtest request for a given symbol (see
        :mod:`msai.services.nautilus.catalog_builder`) and is read directly
        by ``BacktestNode`` during backtest execution.
        """
        return self.data_root / "nautilus"


settings: Settings = Settings()
