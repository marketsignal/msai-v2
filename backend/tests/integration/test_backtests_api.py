"""Integration tests for the backtests API — auto-heal lifecycle fields.

Task B9 (backtest-auto-ingest-on-missing-data): verify that
``GET /api/v1/backtests/{id}/status`` and ``GET /api/v1/backtests/history``
surface the ``phase`` + ``progress_message`` columns on the ``Backtest``
row when the auto-heal cycle has populated them.

The API layer uses ``response_model_exclude_none=True`` on both
endpoints (PR #39 contract), so absent values stay ABSENT in the JSON
response — preserving backward compat for older callers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app
from msai.models.backtest import Backtest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    import httpx


# ---------------------------------------------------------------------------
# Helpers — local to keep the integration surface self-contained. Mirrors
# the shape of ``tests/unit/conftest.py::_make_backtest`` but independent
# so a future rename of that helper doesn't silently break these tests.
# ---------------------------------------------------------------------------


def _make_running_backtest_with_phase(
    *,
    phase: str | None,
    progress_message: str | None,
) -> Backtest:
    return Backtest(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_code_hash="x" * 64,
        config={},
        instruments=["AAPL.NASDAQ"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 15),
        status="running",
        progress=50,
        error_code="unknown",
        phase=phase,
        progress_message=progress_message,
        created_at=datetime.now(UTC),
    )


def _mock_session_returning(row: Backtest) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.get.return_value = row
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [row]
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one_or_none.return_value = row
    # ``len(items)`` on the list path goes through ``func.count()``, which
    # uses ``scalar_one``. Match the single-row count.
    mock_result.scalar_one.return_value = 1
    session.execute.return_value = mock_result
    return session


@pytest.fixture
def seed_running_backtest_awaiting_data() -> Generator[Backtest, None, None]:
    """Seed a single ``running`` row with ``phase='awaiting_data'``.

    Installs a ``get_db`` override yielding an AsyncMock session preloaded
    with the row. The root ``client`` fixture picks up the override
    transparently.
    """
    row = _make_running_backtest_with_phase(
        phase="awaiting_data",
        progress_message="Downloading AAPL...",
    )
    session = _mock_session_returning(row)

    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db] = _override
    try:
        yield row
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# GET /api/v1/backtests/{id}/status
# ---------------------------------------------------------------------------


async def test_status_endpoint_returns_phase_when_set(
    client: httpx.AsyncClient,
    seed_running_backtest_awaiting_data: Backtest,
) -> None:
    """``phase`` + ``progress_message`` surface in the /status JSON body
    when the row has them populated.
    """
    row = seed_running_backtest_awaiting_data

    async with client as ac:
        response = await ac.get(f"/api/v1/backtests/{row.id}/status")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "running"
    assert body["phase"] == "awaiting_data"
    assert body["progress_message"] == "Downloading AAPL..."


# ---------------------------------------------------------------------------
# GET /api/v1/backtests/history
# ---------------------------------------------------------------------------


async def test_history_endpoint_returns_phase_when_set(
    client: httpx.AsyncClient,
    seed_running_backtest_awaiting_data: Backtest,
) -> None:
    """The list endpoint mirrors /status — lifecycle fields ride on each
    ``BacktestListItem`` so the list page can render the "Fetching data…"
    badge (Task F2 wiring).
    """
    async with client as ac:
        response = await ac.get("/api/v1/backtests/history")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["status"] == "running"
    assert item["phase"] == "awaiting_data"
    assert item["progress_message"] == "Downloading AAPL..."
