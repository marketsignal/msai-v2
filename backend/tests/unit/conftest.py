"""Shared unit-test fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app
from msai.models.backtest import Backtest


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
# Failed-backtest seed fixtures (Task B0 — backtest-failure-surfacing)
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
    mock_result.scalar_one.return_value = 1
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

    async def _override() -> AsyncGenerator[AsyncMock, None]:
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

    async def _override() -> AsyncGenerator[AsyncMock, None]:
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

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield str(row.id)
    finally:
        app.dependency_overrides.pop(get_db, None)
