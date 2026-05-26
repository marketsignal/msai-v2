"""Integration tests for ``GET /api/v1/backtests/history`` smoke filtering.

Verifies the ``smoke_only`` + ``include_smoke`` query-parameter contract
introduced for the ingest-backtest-smoke-test feature (Task 9).

The endpoint must honor smoke filtering on BOTH the single-strategy
``Backtest.smoke`` column AND the new ``PortfolioRun.smoke`` column,
across all three ``type`` branches (``single`` / ``portfolio`` / ``all``).

Pagination correctness depends on the row-fetch query AND the count
query AND (for ``type="all"``) the per-table total queries all applying
the same filter clauses. The tests below assert ``total`` values
explicitly to catch the "filter only the row-fetch" bug class.

Filter semantics:

- ``smoke_only=true`` → only ``smoke=True`` rows of the queried type(s).
- ``smoke_only=false`` (default) AND ``include_smoke=false`` (default) →
  smoke rows are excluded.
- ``include_smoke=true`` → smoke + non-smoke rows.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_history_default_excludes_smoke_portfolio_runs(
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
):
    """Default response (no smoke flags) hides smoke portfolio runs."""
    # Arrange — one non-smoke single backtest, one non-smoke portfolio run,
    # one smoke portfolio run.
    await make_backtest()
    await make_completed_portfolio_run()
    await make_completed_portfolio_run(smoke=True)

    # Act — default behavior is "hide smoke".
    response = await api_client_authed.get("/api/v1/backtests/history?type=all")

    # Assert — no row carries smoke=True; total reflects only non-smoke rows.
    assert response.status_code == 200, response.text
    body = response.json()
    assert all(item["smoke"] is False for item in body["items"])
    # 1 single + 1 portfolio (smoke one is excluded).
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_history_smoke_only_portfolio_returns_only_smoke_portfolio_rows(
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
):
    """``smoke_only=true&type=portfolio`` returns ONLY smoke portfolio rows.

    Confirms the filter is applied to BOTH ``portfolio_query`` AND
    ``portfolio_count_query`` — if only the row-fetch were filtered the
    response ``total`` would still include the non-smoke portfolio run.
    """
    # Arrange — seed both flavors so the filter is provably narrowing.
    await make_backtest()  # single, non-smoke — should never appear
    await make_completed_portfolio_run()  # portfolio, non-smoke
    smoke_run = await make_completed_portfolio_run(smoke=True)
    await make_completed_portfolio_run(smoke=True)

    # Act
    response = await api_client_authed.get(
        "/api/v1/backtests/history?type=portfolio&smoke_only=true",
    )

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2, body
    types = {item["type"] for item in body["items"]}
    assert types == {"portfolio"}
    assert all(item["smoke"] is True for item in body["items"])
    # The seeded smoke run is in the result set.
    returned_ids = {item["id"] for item in body["items"]}
    assert str(smoke_run.id) in returned_ids


@pytest.mark.asyncio
async def test_history_smoke_only_all_returns_smoke_rows_from_both_branches(
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
):
    """``smoke_only=true&type=all`` returns smoke rows from BOTH tables.

    Confirms the filter wires through the merged ``type="all"`` path —
    including ``single_total`` and ``portfolio_total`` which together
    drive the response ``total`` for pagination.
    """
    # Arrange — one smoke + one non-smoke on each side.
    await make_backtest()  # single non-smoke
    await make_backtest(smoke=True)  # single smoke
    await make_completed_portfolio_run()  # portfolio non-smoke
    await make_completed_portfolio_run(smoke=True)  # portfolio smoke

    # Act
    response = await api_client_authed.get(
        "/api/v1/backtests/history?type=all&smoke_only=true",
    )

    # Assert — only the two smoke rows (one per side).
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2, body
    assert all(item["smoke"] is True for item in body["items"])
    types = {item["type"] for item in body["items"]}
    assert types == {"single", "portfolio"}


@pytest.mark.asyncio
async def test_history_include_smoke_returns_smoke_and_non_smoke(
    api_client_authed,
    make_backtest,
    make_completed_portfolio_run,
):
    """``include_smoke=true`` surfaces both smoke + non-smoke rows."""
    # Arrange
    await make_backtest()
    await make_backtest(smoke=True)
    await make_completed_portfolio_run()
    await make_completed_portfolio_run(smoke=True)

    # Act
    response = await api_client_authed.get(
        "/api/v1/backtests/history?type=all&include_smoke=true",
    )

    # Assert — all four rows visible, with smoke flag faithful to source.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4, body
    smoke_flags = {item["smoke"] for item in body["items"]}
    assert smoke_flags == {True, False}
