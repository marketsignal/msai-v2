"""F4 -- Full-mode smoke test (walk-forward + Optuna, capped at 2 trials).

The test exercises the full Full-mode path:

1. ``POST /api/v1/portfolios`` (strategy_ids bridge).
2. ``POST .../runs`` with ``mode=full``.
3. Drive the worker manually (no arq in the integration suite).  The
   optimizer's ``run_portfolio_walk_forward`` runs end-to-end: Optuna
   ask/tell loop, returns-aggregation trial body, IS/OOS scoring,
   walk-forward payload assembly.
4. Poll ``GET .../runs/{id}`` for terminal, then assert IS/OOS / mode /
   optimization_trace shape.

The returns-aggregation trial body (see ``_run_full_mode`` docstring in
``orchestration.py``) keeps wall-clock under control without spinning
up Nautilus per trial.

Marked ``slow`` so the default suite filter can skip it; the suite
should still finish under 2 minutes with the 2-trial cap and ~2
walk-forward windows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from msai.core.config import settings
from msai.services.nautilus.backtest_runner import BacktestResult
from msai.services.portfolio import PortfolioService

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from msai.models.strategy import Strategy


class _StubRunner:
    """Stand-in for BacktestRunner.run — used by the Full-mode cache build.

    Phase 5.1 P0-A: ``_run_full_mode`` now runs every member through the
    Quick-mode backtest path once to populate a real per-strategy returns
    cache before the optimizer trial loop. This stub returns a deterministic
    10-bar returns frame so the cache build doesn't touch Parquet IO.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> BacktestResult:
        self.calls.append(kwargs)
        timestamps = pd.date_range("2023-01-02", periods=10, freq="D", tz="UTC")
        return BacktestResult(
            orders_df=pd.DataFrame(),
            positions_df=pd.DataFrame(),
            account_df=pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "returns": [
                        0.01,
                        -0.005,
                        0.008,
                        0.003,
                        -0.002,
                        0.004,
                        0.006,
                        -0.001,
                        0.002,
                        0.005,
                    ],
                    "equity": [100_000.0 * (1.0 + 0.01 * (i + 1)) for i in range(10)],
                }
            ),
            metrics={"total_return": 0.05, "sharpe": 1.2},
        )


async def _poll_until_terminal(
    client: httpx.AsyncClient,
    run_id: str,
    *,
    max_attempts: int = 60,
) -> dict[str, Any]:
    """Poll until the run reaches a terminal state."""
    for _ in range(max_attempts):
        resp = await client.get(f"/api/v1/portfolios/runs/{run_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in ("completed", "failed", "canceled"):
            return body
    raise AssertionError(f"Run {run_id} did not reach a terminal state after {max_attempts} polls")


@pytest.mark.asyncio
@pytest.mark.slow
async def test_full_mode_smoke(
    api_client_authed: httpx.AsyncClient,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_strategy: Callable[..., Awaitable[Strategy]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Full-mode walk-forward + Optuna smoke, capped at 2 trials."""
    # Arrange -- cap the trial budget on the settings singleton so the
    # smoke stays under wall-clock cap.  ``settings`` is imported at
    # module-top throughout the codebase; mutating the attribute here is
    # the supported override pattern (the env-var override goes through
    # the same attribute via pydantic-settings validation).
    monkeypatch.setattr(settings, "portfolio_full_trial_count", 2)

    s1 = await make_strategy(name="full-smoke-s1")

    mock_pool = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "fake-job-id"
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    # Act -- compose the portfolio.
    create_resp = await api_client_authed.post(
        "/api/v1/portfolios",
        json={
            "name": "full-smoke",
            "objective": "maximize_sharpe",
            "base_capital": 100_000.0,
            "max_position_size": 0.25,
            "max_drawdown_halt": 0.20,
            "default_mode": "full",
            "allocator_name": "equal_weight",
            "strategy_ids": [str(s1.id)],
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    portfolio = create_resp.json()

    # Act -- start a Full-mode run.  Date window sized so the optimizer's
    # default walk-forward windows (train=252d, test=63d, step=63d) fit
    # in at least one full IS+OOS pair.  2023-01-01..2024-06-01 = 517d,
    # one rolling 252+63 window plus a step or two.
    with patch(
        "msai.api.portfolio.get_redis_pool",
        new=AsyncMock(return_value=mock_pool),
    ):
        run_resp = await api_client_authed.post(
            f"/api/v1/portfolios/{portfolio['id']}/runs",
            json={
                "start_date": "2023-01-01",
                "end_date": "2024-06-01",
                "mode": "full",
            },
        )
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()
    assert run["mode"] == "full"

    # Drive the worker manually -- arq isn't running.  Full mode now caches
    # REAL per-strategy returns (Phase 5.1 P0-A fix) by running each member
    # through the Quick-mode backtest path before the optimizer loop.  We
    # stub the catalog warmup + the runner so the cache build stays off
    # Parquet IO and Nautilus subprocesses; the optimizer's trial body then
    # consumes those cached returns end-to-end.
    def _fake_ensure(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure,
    )

    svc = PortfolioService()
    await svc.run_portfolio_backtest(
        run["id"],
        runner=_StubRunner(),
        session_factory=portfolio_session_factory,
    )

    # Assert -- poll the public endpoint until terminal.
    final = await _poll_until_terminal(api_client_authed, run["id"])
    assert final["status"] == "completed", final
    assert final["mode"] == "full"

    # IS/OOS scalars persisted (Numeric(18,6) → float on response).
    assert final["is_metric"] is not None
    assert final["oos_metric"] is not None

    # Optimization trace populated (one entry per completed/pruned trial).
    assert final["optimization_trace"] is not None
    assert len(final["optimization_trace"]) >= 1

    # Walk-forward payload captures the per-window breakdown.
    assert final["walk_forward_payload"] is not None
    assert "windows" in final["walk_forward_payload"]
    assert len(final["walk_forward_payload"]["windows"]) >= 1


@pytest.mark.asyncio
@pytest.mark.slow
async def test_full_mode_honors_n_trials_override(
    api_client_authed: httpx.AsyncClient,
    portfolio_session_factory: async_sessionmaker[AsyncSession],
    make_strategy: Callable[..., Awaitable[Strategy]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``n_trials`` request body field caps the optimizer's trial budget.

    Regression for the FAIL_STALE classification on UC-PB-API-003: there
    was no API-level mechanism to cap Full-mode trials inside a smoke time
    budget — the test had to monkeypatch the settings singleton, which
    doesn't translate to a real verify-e2e run that goes through HTTP.

    Set ``portfolio_full_trial_count`` to a HIGH default (100) and the
    request override to a LOW value (2).  Assert the resulting
    ``optimization_trace`` length reflects the override, NOT the settings
    default.
    """
    # Arrange — production default is high; the per-run override must win.
    monkeypatch.setattr(settings, "portfolio_full_trial_count", 100)

    s1 = await make_strategy(name="full-override-s1")

    mock_pool = AsyncMock()
    mock_job = MagicMock()
    mock_job.job_id = "fake-job-id"
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    create_resp = await api_client_authed.post(
        "/api/v1/portfolios",
        json={
            "name": "full-override",
            "objective": "maximize_sharpe",
            "base_capital": 100_000.0,
            "max_position_size": 0.25,
            "max_drawdown_halt": 0.20,
            "default_mode": "full",
            "allocator_name": "equal_weight",
            "strategy_ids": [str(s1.id)],
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    portfolio = create_resp.json()

    # Act — start a Full-mode run with ``n_trials=2`` in the request body.
    with patch(
        "msai.api.portfolio.get_redis_pool",
        new=AsyncMock(return_value=mock_pool),
    ):
        run_resp = await api_client_authed.post(
            f"/api/v1/portfolios/{portfolio['id']}/runs",
            json={
                "start_date": "2023-01-01",
                "end_date": "2024-06-01",
                "mode": "full",
                "n_trials": 2,
            },
        )
    assert run_resp.status_code == 201, run_resp.text
    run = run_resp.json()

    # Drive the worker manually (same plumbing as test_full_mode_smoke).
    def _fake_ensure(
        *,
        symbols: list[str],
        raw_parquet_root: Any,
        catalog_root: Any,
        asset_class: str,
    ) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    monkeypatch.setattr(
        "msai.services.portfolio.orchestration.ensure_catalog_data",
        _fake_ensure,
    )

    svc = PortfolioService()
    await svc.run_portfolio_backtest(
        run["id"],
        runner=_StubRunner(),
        session_factory=portfolio_session_factory,
    )

    # Assert — final trace length reflects the per-run override.  The
    # optimizer splits the trial budget across walk-forward windows
    # (``trials_this_window = max(1, n_trials // total_windows)``), so the
    # total trial count is at least ``n_trials`` but no more than
    # ``n_trials + total_windows`` (rounding-up across windows).  For a
    # 517-day window with the default 252/63/63 walk-forward params,
    # ``total_windows = 4``-ish — well below the settings default of 100.
    final = await _poll_until_terminal(api_client_authed, run["id"])
    assert final["status"] == "completed", final
    assert final["optimization_trace"] is not None
    trace_len = len(final["optimization_trace"])
    # n_trials=2 produces at most ``max(1, 2 // total_windows) * total_windows``
    # trials.  With total_windows ≤ 6 and the optimizer's per-window
    # ``max(1, n_trials // total_windows)`` clamp, the upper bound is
    # ``1 * total_windows ≤ 6`` and the lower bound is 1.  Crucially,
    # without the override the trace would hit ``100 // total_windows *
    # total_windows ≈ 96-100`` — the assertion catches a regression where
    # the override is ignored.
    assert trace_len < 50, (
        f"n_trials=2 override should cap trace well below the 100-trial "
        f"settings default, got {trace_len}"
    )
    assert trace_len >= 1, "Optimizer must run at least one trial"
