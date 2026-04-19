"""Application configuration loaded from environment variables.

Uses pydantic-settings to load configuration from environment variables and
an optional ``.env`` file. A module-level ``settings`` singleton is provided
for convenient import across the codebase.
"""

from __future__ import annotations

from os import cpu_count
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
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
    # Accept both the legacy names (``IB_HOST`` / ``IB_PORT`` — used
    # by unit tests and local-dev setups) and the Docker-compose
    # names (``IB_GATEWAY_HOST`` / ``IB_GATEWAY_PORT_PAPER`` — set on
    # every service container). Before this alias, a backend running
    # under ``docker compose`` saw only ``IB_GATEWAY_HOST`` and fell
    # through to the 127.0.0.1:4002 defaults, so ``/account/health``
    # probed localhost of its own container instead of the gateway
    # container and always reported unreachable. Drill 2026-04-15.
    #
    # The legacy name is listed first so an operator-set ``IB_HOST``
    # overrides the compose-wide default without editing compose
    # files — useful when pointing a dev backend at a remote gateway.
    # Live-port deployments set ``IB_PORT=4003`` explicitly because
    # this alias only picks up the PAPER variant by design (the
    # supervisor's paper/live mismatch guard refuses to cross-wire).
    ib_host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("IB_HOST", "IB_GATEWAY_HOST"),
    )
    ib_port: int = Field(
        default=4002,
        validation_alias=AliasChoices("IB_PORT", "IB_GATEWAY_PORT_PAPER"),
    )

    # ------------------------------------------------------------------
    # IB short-lived-client tunables (used by `msai instruments refresh`
    # and any other one-shot IB connection that isn't a live subprocess).
    # ------------------------------------------------------------------

    # Wall-clock budget for the IB Gateway TCP connection + client-ready
    # probe. Intentionally separate from ``ib_request_timeout_seconds``
    # so a dead gateway fails fast (~5s) while slow individual
    # qualifications still honor the longer per-request timeout.
    ib_connect_timeout_seconds: int = Field(
        default=5,
        validation_alias=AliasChoices("IB_CONNECT_TIMEOUT_SECONDS"),
    )

    # Post-connect per-request timeout for IB contract qualification
    # (``reqContractDetails`` round-trip). ``int`` matches Nautilus
    # ``get_cached_ib_client(request_timeout_secs=...)`` signature.
    ib_request_timeout_seconds: int = Field(
        default=30,
        validation_alias=AliasChoices("IB_REQUEST_TIMEOUT_SECONDS"),
    )

    # Pragmatic default IB ``client_id`` for ``msai instruments
    # refresh``. Live subprocesses derive their client_id from a 31-bit
    # hash of the deployment slug (``live_node_config.py::
    # _derive_client_id``), so collision with 999 is mathematically
    # possible but extremely unlikely. Surfaced in CLI help + every
    # preflight log so the operator sees which id the CLI is using.
    # See nautilus.md gotcha #3 — two clients on the same ``client_id``
    # silently disconnect each other.
    ib_instrument_client_id: int = Field(
        default=999,
        validation_alias=AliasChoices("IB_INSTRUMENT_CLIENT_ID"),
    )

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

    # Daily ingest scheduling (Phase 2 #3 — Codex parity port).
    # The arq cron triggers `run_nightly_ingest_if_due` every minute;
    # the wrapper checks these settings + a JSON state file to decide
    # whether to actually run today. Operators in non-US markets can set
    # the timezone (LSE 16:30 London, TSE 15:00 Tokyo) without a code
    # change; setting `daily_ingest_enabled=false` disables the cron
    # without removing the job from the worker.
    daily_ingest_enabled: bool = True
    daily_ingest_timezone: str = "America/New_York"  # default: post-US-close ingest
    # Range-validated so an out-of-range env var (e.g. HOUR=25) fails fast
    # at config load with a clear pydantic ValidationError rather than
    # crashing every cron tick inside `_is_due` at datetime.replace.
    daily_ingest_hour: int = Field(default=18, ge=0, le=23)
    daily_ingest_minute: int = Field(default=0, ge=0, le=59)
    # Session-date offset for the target ingest. Default 0 assumes a
    # post-close same-day schedule: 18:00 ET on April 14 ingests
    # April 14's session. If operators move to an overnight schedule
    # (e.g. 02:00 local-tz the morning after), set this to -1 so the
    # wrapper ingests yesterday's session instead of today's. Must be
    # non-positive — ingesting future sessions is never correct.
    daily_ingest_session_offset_days: int = Field(default=0, ge=-7, le=0)

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
    def scheduler_state_path(self) -> Path:
        """JSON state file for the daily ingest scheduler.

        Records ``last_enqueued_date`` so the cron wrapper is idempotent
        across worker restarts and concurrent fires — only one ingest
        per scheduled-tz calendar day. Stored under ``data_root`` so it
        survives container rebuilds via the bind-mounted volume.
        """
        return self.data_root / "scheduler" / "daily_ingest_state.json"

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

    @property
    def databento_definition_root(self) -> Path:
        """Root directory for cached Databento ``.definition.dbn.zst`` payloads
        (``{data_root}/databento_definitions``).

        Used by :meth:`SecurityMaster._resolve_databento_continuous` as the
        destination for on-demand ``fetch_definition_instruments``
        downloads. Layout: ``{root}/{dataset}/{raw_symbol}/{start}_{end}.definition.dbn.zst``.
        Persisted under ``data_root`` so the files survive container rebuilds
        via the bind-mounted volume and subsequent backtests of the same
        continuous symbol can reuse the cached definition.
        """
        return self.data_root / "databento_definitions"

    @property
    def alerts_path(self) -> Path:
        """File-backed operational alert history (``{data_root}/alerts/alerts.json``).

        Written by :class:`msai.services.alerting.AlertingService` and read
        by the ``/api/v1/alerts/`` router for the dashboard audit trail.
        Capped at 200 records (newest first).
        """
        return self.data_root / "alerts" / "alerts.json"


settings: Settings = Settings()
