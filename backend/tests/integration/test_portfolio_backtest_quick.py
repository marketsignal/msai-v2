"""F3 — Quick-mode end-to-end via the public API.

Flow:

1. ``POST /api/v1/portfolios`` with ``strategy_ids`` (F1c bridge path).
2. ``POST /api/v1/portfolios/{id}/runs`` with ``mode=quick``.
3. Drive the worker directly (no arq) — patches the Quick path's heavy
   dependencies (Nautilus runner, ReportGenerator, catalog warmup) so the
   test exercises the orchestration layer's mode branch + persistence
   without spawning subprocesses.
4. Poll ``GET /api/v1/portfolios/runs/{id}`` until the row is terminal,
   then assert metrics + series + mode.

ARRANGE goes through the public API (``POST /portfolios`` + ``POST
.../runs``); VERIFY goes through the public API (``GET .../runs/{id}``).
The arq worker would normally pick the job up; we substitute the
service call directly because no arq runtime is configured in the
integration suite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from msai.services.nautilus.backtest_runner import BacktestResult
from msai.services.portfolio import PortfolioService

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from msai.models.strategy import Strategy


# ---------------------------------------------------------------------------
# Test doubles -- mirror the shape used by test_portfolio_quick_mode.py
# ---------------------------------------------------------------------------


def _canned_account_df() -> pd.DataFrame:
    timestamps = pd.date_range("2024-01-02", periods=10, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "returns": [0.01, -0.005, 0.008, 0.003, -0.002, 0.004, 0.006, -0.001, 0.002, 0.005],
            "equity": [100_000.0 * (1.0 + 0.01 * (i + 1)) for i in range(10)],
        }
    )


class _StubRunner:
    """Pretend BacktestRunner — returns a canned BacktestResult."""

    def run(self, **kwargs: Any) -> BacktestResult:
        return BacktestResult(
            orders_df=pd.DataFrame(),
            positions_df=pd.DataFrame(),
            account_df=_canned_account_df(),
            metrics={"total_return": 0.05, "sharpe": 1.2},
        )


class _StubReportGen:
    def __init__(self, tmp_path: Any) -> None:
        self.tmp_path = tmp_path

    def generate_tearsheet(
        self,
        returns: Any,
        benchmark: Any = None,
        title: str = "MSAI Backtest Report",
    ) -> str:
        return "<html>tearsheet</html>"

    def save_report(self, html: str, backtest_id: str, data_root: str) -> str:
        out = self.tmp_path / f"{backtest_id}.html"
        out.write_text(html)
        return str(out)


async def _poll_until_terminal(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    max_attempts: int = 30,
) -> dict[str, Any]:
    """Poll /runs/{id} until the response body's status is terminal.

    The integration suite doesn't run an arq worker so callers must
    invoke ``PortfolioService.run_portfolio_backtest`` themselves before
    calling this helper.  The poll is here so the test reads like a real
    user use case — POST → poll → GET.
    """
    for _ in range(max_attempts):
        resp = await client.get(f"/api/v1/portfolios/runs/{run_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in ("completed", "failed", "canceled"):
            return body
    raise AssertionError(f"Run {run_id} did not reach a terminal state after {max_attempts} polls")


@pytest.mark.asyncio
async def test_quick_mode_e2e(
    api_client_authed: httpx.AsyncClient,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_strategy: Callable[..., Awaitable[Strategy]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """End-to-end Quick mode via the public API."""
    # Arrange -- two strategies, Redis enqueue mocked.
    s1 = await make_strategy(name="quick-s1")
    s2 = await make_strategy(name="quick-s2")

    mock_pool = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "fake-job-id"
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    # Quick path warms the Nautilus catalog from Parquet — short-circuit
    # so the test stays isolated from disk IO.
    def _fake_ensure_catalog(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure_catalog,
    )

    # Act -- compose the portfolio (strategy_ids → F1c bridge → default
    # candidates → allocations).
    create_resp = await api_client_authed.post(
        "/api/v1/portfolios",
        json={
            "name": "quick-e2e",
            "objective": "maximize_sharpe",
            "base_capital": 100_000.0,
            "max_position_size": 0.25,
            "max_drawdown_halt": 0.20,
            "default_mode": "quick",
            "allocator_name": "equal_weight",
            "strategy_ids": [str(s1.id), str(s2.id)],
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    portfolio = create_resp.json()

    # Act -- start a Quick-mode run.
    with patch(
        "msai.api.portfolio.get_redis_pool",
        new=AsyncMock(return_value=mock_pool),
    ):
        run_resp = await api_client_authed.post(
            f"/api/v1/portfolios/{portfolio['id']}/runs",
            json={
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "mode": "quick",
            },
        )
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["status"] == "pending"
    assert run["mode"] == "quick"

    # Drive the worker manually -- arq isn't running in the test suite.
    # We substitute the Nautilus runner + report generator so the test
    # doesn't shell out to subprocesses.  The orchestration layer's
    # mode-branch is exercised end-to-end through this path.
    svc = PortfolioService()
    await svc.run_portfolio_backtest(
        run["id"],
        runner=_StubRunner(),
        report_generator=_StubReportGen(tmp_path),
        session_factory=portfolio_session_factory,
    )

    # Assert -- poll the public endpoint until terminal.
    final = await _poll_until_terminal(api_client_authed, run["id"])
    assert final["status"] == "completed", final
    assert final["mode"] == "quick"
    # Quick mode persists metrics + series + allocations on the run row.
    assert final["metrics"] is not None
    assert "sharpe" in final["metrics"]
    assert final["series"] is not None
    assert len(final["series"]) > 0
    assert final["allocations"] is not None
    assert len(final["allocations"]) == 2
    # IS/OOS are Full-mode only — must be None on a Quick run.
    assert final["is_metric"] is None
    assert final["oos_metric"] is None


@pytest.mark.asyncio
async def test_quick_mode_with_two_member_strategies_completes(
    api_client_authed: httpx.AsyncClient,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_strategy: Callable[..., Awaitable[Strategy]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Regression for E2E iter-2 FAIL_BUG #1: multi-strategy Quick run.

    The original bug: a member strategy whose stored candidate config
    contained ``order_id_tag=""`` would crash the Nautilus backtest
    subprocess (Rust ``StrategyId`` validator rejects ``"Strategy-"`` as
    an invalid identifier with an empty tag part).  The subprocess
    panicked before its Python error-handler could write a result pickle,
    so the parent ``BacktestRunner`` saw an empty file and raised an
    opaque ``EOFError: Ran out of input`` — surfaced via the per-strategy
    attribution table as ``{"error_type": "EOFError", "message": "Ran out
    of input"}``.

    Fix: ``_prepare_strategy_config`` (both
    ``services/portfolio/orchestration.py`` and
    ``workers/backtest_job.py``) now strips an empty ``order_id_tag`` so
    Nautilus falls back to its base default (``None``), which the
    validator accepts.  This test seeds two candidates that each carry
    ``order_id_tag=""`` in their stored config — the exact shape that
    bit E2E iter-2 — and asserts the run completes cleanly.
    """
    # Arrange — two strategies, each with an empty-order-id-tag in
    # ``default_config`` (which the bridge copies into the candidate).
    s1 = await make_strategy(
        name="quick-multi-s1",
        default_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-DAY-LAST-EXTERNAL",
            "order_id_tag": "",  # ← the exact poison that triggered EOFError
        },
    )
    s2 = await make_strategy(
        name="quick-multi-s2",
        default_config={
            "instrument_id": "MSFT.NASDAQ",
            "bar_type": "MSFT.NASDAQ-1-DAY-LAST-EXTERNAL",
            "order_id_tag": "",
        },
    )

    mock_pool = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "fake-job-id"
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    def _fake_ensure_catalog(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure_catalog,
    )

    # Intercept the strategy_config passed to the stub runner so we can
    # assert the scrub happened.  Without the fix, ``order_id_tag=""``
    # would still be present in the resolved config that flows into
    # Nautilus.
    seen_configs: list[dict[str, Any]] = []

    class _CapturingRunner:
        def run(self, **kwargs: Any) -> BacktestResult:
            seen_configs.append(dict(kwargs.get("strategy_config", {})))
            return _StubRunner().run(**kwargs)

    create_resp = await api_client_authed.post(
        "/api/v1/portfolios",
        json={
            "name": "quick-multi",
            "objective": "maximize_sharpe",
            "base_capital": 100_000.0,
            "default_mode": "quick",
            "allocator_name": "equal_weight",
            "strategy_ids": [str(s1.id), str(s2.id)],
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    portfolio = create_resp.json()

    with patch(
        "msai.api.portfolio.get_redis_pool",
        new=AsyncMock(return_value=mock_pool),
    ):
        run_resp = await api_client_authed.post(
            f"/api/v1/portfolios/{portfolio['id']}/runs",
            json={
                "start_date": "2024-01-01",
                "end_date": "2024-06-01",
                "mode": "quick",
            },
        )
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()

    svc = PortfolioService()
    await svc.run_portfolio_backtest(
        run["id"],
        runner=_CapturingRunner(),
        report_generator=_StubReportGen(tmp_path),
        session_factory=portfolio_session_factory,
    )

    final = await _poll_until_terminal(api_client_authed, run["id"])
    # Assert — completed (NOT failed with EOFError per-strategy error).
    assert final["status"] == "completed", final

    # Assert — every strategy_config that flowed to the runner had its
    # empty ``order_id_tag`` scrubbed (the actual fix verification).
    assert len(seen_configs) == 2, seen_configs
    for cfg in seen_configs:
        assert "order_id_tag" not in cfg, f"Empty order_id_tag survived into the runner: {cfg}"
