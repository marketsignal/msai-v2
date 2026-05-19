"""G4 integration tests — unified ``GET /api/v1/backtests/history``.

Verifies:

- The history endpoint surfaces BOTH single-strategy Backtests AND
  PortfolioRuns when no ``type`` filter is supplied (default ``all``).
- ``?type=portfolio`` restricts to PortfolioRun rows; ``?type=single``
  restricts to Backtest rows.

Each row carries a ``type`` discriminator field so frontend table code
can route per-row rendering (single vs portfolio columns) without needing
a separate API call.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_history_returns_both_single_and_portfolio_rows(
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
):
    # Arrange — seed one of each.
    await make_backtest()
    await make_completed_portfolio_run()

    # Act
    response = await api_client_authed.get("/api/v1/backtests/history")

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    types = {item["type"] for item in body["items"]}
    assert "single" in types
    assert "portfolio" in types


@pytest.mark.asyncio
async def test_history_type_filter_returns_only_portfolio(
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
):
    # Arrange
    await make_backtest()
    await make_completed_portfolio_run()

    # Act
    response = await api_client_authed.get(
        "/api/v1/backtests/history?type=portfolio",
    )

    # Assert
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        assert item["type"] == "portfolio"


@pytest.mark.asyncio
async def test_history_type_filter_returns_only_single(
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
):
    # Arrange
    await make_backtest()
    await make_completed_portfolio_run()

    # Act
    response = await api_client_authed.get(
        "/api/v1/backtests/history?type=single",
    )

    # Assert
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        assert item["type"] == "single"


@pytest.mark.asyncio
async def test_history_rejects_page_above_cap(
    api_client_authed,
) -> None:
    """Codex bot iter-13 P1 on PR #73 — ``type="all"`` fetches
    ``page * page_size`` rows per side, so unbounded ``page`` is a DoS
    surface (deep page → large table scan + in-memory sort). The
    Query(le=100) cap rejects ``page > 100`` at the FastAPI layer.
    """
    response = await api_client_authed.get(
        "/api/v1/backtests/history",
        params={"page": 1000, "page_size": 100, "type": "all"},
    )
    # FastAPI returns 422 for query-validation failures.
    assert response.status_code == 422, response.text
    body = response.json()
    detail_str = str(body.get("detail", ""))
    assert "page" in detail_str.lower(), body
