"""Shared unit-test fixtures."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pandas as pd
import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app
from msai.models.backtest import Backtest
from msai.models.trade import Trade

# Reconfigure structlog without caching so structlog.testing.capture_logs()
# can intercept the processor chain on loggers that were already bound at
# module import time. `msai.main` calls setup_logging() with
# cache_logger_on_first_use=True, which freezes the chain on first log
# call and makes capture_logs() see an empty list in CI (order-dependent
# test flakiness). See tests/unit/test_backtest_job.py and
# tests/unit/services/backtests/test_auto_heal.py for the consumers.
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(10),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=False,
    context_class=dict,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable


@pytest.fixture(autouse=True)
def _clear_ib_factory_globals():
    """Clear Nautilus IB adapter factory globals between tests.

    Rationale (research brief finding #3): Nautilus 1.223.0 caches
    clients/providers in module-level dicts that have no ``.clear()``
    helper. Between unit tests that touch ``get_cached_ib_client`` or
    ``get_cached_interactive_brokers_instrument_provider``, a stale
    cached client from an earlier test can leak into a later one.
    Production is unaffected because each ``msai instruments refresh``
    invocation is a fresh process.

    We don't clear ``GATEWAYS`` because that dict is only populated
    when ``dockerized_gateway=...`` is passed to
    ``get_cached_ib_client`` — the CLI never does, so the dict stays
    empty in our test paths.

    Runs on every unit test (autouse) but the clear is cheap: the
    dicts are empty when untouched.
    """
    yield
    try:
        from nautilus_trader.adapters.interactive_brokers import factories
    except ImportError:
        # Running without Nautilus installed (some CI jobs skip heavy
        # deps). No globals to clear.
        return
    factories.IB_CLIENTS.clear()
    factories.IB_INSTRUMENT_PROVIDERS.clear()


# ---------------------------------------------------------------------------
# Failed-backtest seed fixtures
# ---------------------------------------------------------------------------
#
# These fixtures install an override for ``get_db`` that yields an AsyncMock
# session pre-seeded with a specific ``Backtest`` row. The shared ``client``
# fixture in ``backend/tests/conftest.py`` picks up that override transparently
# so API tests can hit the real routers without needing Postgres.
#
# NOTE: Until Tasks B4+B5 add the ``error_code``/``error_public_message``/
# ``error_suggested_action``/``error_remediation`` columns, constructing a
# ``Backtest`` row with those kwargs will raise ``TypeError: 'error_code' is an
# invalid keyword argument for Backtest``. That's the intentional TDD "red"
# state for B0 — the smoke test in ``test_backtest_fixtures.py`` turns green
# once B4+B5 land.


def _make_backtest(**overrides: object) -> Backtest:
    base: dict[str, object] = dict(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_code_hash="x" * 64,
        config={},
        instruments=["ES.n.0"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 15),
        status="pending",
        progress=0,
        # [B8] BacktestListItem requires created_at non-null; populate
        # here so the history endpoint's pydantic validation accepts
        # the row. Per-fixture overrides can still set a different value.
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return Backtest(**base)


def _mock_session_returning(row: Backtest) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = row
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [row]
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = row
    session.execute.return_value = mock_result
    return session


@pytest.fixture
async def seed_failed_backtest() -> AsyncGenerator[tuple[str, str], None]:
    raw_msg = "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES."
    row = _make_backtest(
        status="failed",
        error_message=raw_msg,
        error_code="missing_data",
        error_public_message="<DATA_ROOT>/parquet/stocks/ES is empty",
        error_suggested_action="Run: msai ingest stocks ES 2025-01-02 2025-01-15",
        error_remediation={
            "kind": "ingest_data",
            "symbols": ["ES.n.0"],
            "asset_class": "stocks",
            "start_date": "2025-01-02",
            "end_date": "2025-01-15",
            "auto_available": False,
        },
        completed_at=datetime.now(UTC),
    )
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield str(row.id), raw_msg
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def seed_historical_failed_row() -> AsyncGenerator[str, None]:
    row = _make_backtest(
        status="failed",
        error_message="some historical error text /app/data/parquet/missing",
        error_code="unknown",
        error_public_message=None,
        error_suggested_action=None,
        error_remediation=None,
        completed_at=datetime.now(UTC),
    )
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield str(row.id)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def seed_pending_backtest() -> AsyncGenerator[str, None]:
    row = _make_backtest(status="pending", error_code="unknown")
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield str(row.id)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Pure-factory helpers for backtest model fixtures
# ---------------------------------------------------------------------------
#
# These build in-memory Python objects only — NO DB persistence. They are
# consumed by unit tests + API-level integration tests that mock sessions
# via ``AsyncMock`` and serve the rows through ``_mock_session_returning``.


def _make_backtest_completed_with_series(**overrides: object) -> Backtest:
    """Completed backtest with ``series_status='ready'`` + canonical payload."""
    default_series: dict[str, object] = {
        "daily": [
            {
                "date": "2024-01-02",
                "equity": 100_500.0,
                "drawdown": 0.0,
                "daily_return": 0.005,
            },
            {
                "date": "2024-01-03",
                "equity": 101_000.0,
                "drawdown": 0.0,
                "daily_return": 0.005,
            },
        ],
        "monthly_returns": [{"month": "2024-01", "pct": 0.01}],
    }
    defaults: dict[str, object] = {
        "status": "completed",
        "metrics": {"sharpe_ratio": 2.1, "total_return": 0.01, "num_trades": 4},
        "report_path": "/tmp/ready-report.html",
        "series": default_series,
        "series_status": "ready",
    }
    defaults.update(overrides)
    return _make_backtest(**defaults)


def _make_backtest_legacy(**overrides: object) -> Backtest:
    """Pre-PR Backtest: ``series=None``, ``series_status='not_materialized'``.

    SQLAlchemy ``server_default`` only applies at DB INSERT time. Pure-factory
    helpers don't round-trip the DB, so we set ``series_status`` explicitly —
    otherwise the attribute would be ``None`` on the returned instance and
    ``_mock_session_returning()`` would serve a row with ``series_status=None``
    to handlers that expect ``"not_materialized"``.
    """
    defaults: dict[str, object] = {
        "status": "completed",
        "metrics": {"sharpe_ratio": 1.2, "total_return": 0.05, "num_trades": 10},
        "report_path": "/tmp/legacy-report.html",
        "series": None,
        "series_status": "not_materialized",
    }
    defaults.update(overrides)
    return _make_backtest(**defaults)


def _make_backtest_failed_series(**overrides: object) -> Backtest:
    """Completed backtest with ``series_status='failed'`` (metrics present, series NULL)."""
    defaults: dict[str, object] = {
        "status": "completed",
        "metrics": {"sharpe_ratio": 0.8, "total_return": 0.02, "num_trades": 6},
        "report_path": "/tmp/fail-report.html",
        "series": None,
        "series_status": "failed",
    }
    defaults.update(overrides)
    return _make_backtest(**defaults)


def _make_backtest_with_trades(n: int) -> tuple[Backtest, list[Trade]]:
    """SYNC factory: in-memory backtest + N individual Trade fills.

    Purposely synchronous — consumed without ``await`` in mocked-session tests.
    Includes ``pnl=None`` on every third row to exercise the coalesce path in
    the API handler.
    """
    bt = _make_backtest(status="completed", metrics={"num_trades": n})
    base_ts = datetime(2024, 1, 2, 9, 30, tzinfo=UTC)
    trades: list[Trade] = []
    for i in range(n):
        t = Trade(
            id=uuid4(),
            backtest_id=bt.id,
            strategy_id=bt.strategy_id,
            strategy_code_hash=bt.strategy_code_hash,
            instrument="SPY.XNAS",
            side="BUY" if i % 2 == 0 else "SELL",
            quantity=Decimal("10"),
            price=Decimal("450.00"),
            pnl=Decimal("5.00") if i % 3 != 0 else None,
            commission=Decimal("0.50"),
            executed_at=base_ts + timedelta(seconds=i),
        )
        trades.append(t)
    return bt, trades


@pytest.fixture
def account_df_factory() -> Callable[..., pd.DataFrame]:
    """Factory: Nautilus-shaped ``account_df`` with tz-aware ``returns`` column."""

    def _factory(periods: int = 21, seed: float = 0.001) -> pd.DataFrame:
        idx = pd.date_range("2024-01-02", periods=periods, freq="B", tz="UTC")
        returns = pd.Series(
            [seed * (1 + i * 0.1) for i in range(periods)],
            index=idx,
            name="returns",
        )
        frame = pd.DataFrame({"returns": returns})
        frame.index = idx
        return frame

    return _factory
