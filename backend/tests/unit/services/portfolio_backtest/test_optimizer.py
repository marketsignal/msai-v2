"""Unit tests for the Full-mode portfolio optimizer driver.

Tests are deliberately shaped to keep Optuna and walk-forward logic exercised
without touching real Nautilus backtests — a stub ``portfolio_backtest_fn`` is
injected so the trial loop can run end-to-end against synthetic metrics.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest

from msai.models.portfolio_enums import PortfolioObjective
from msai.services.portfolio_backtest.optimizer import (
    PortfolioOptimizationResult,
    build_search_space,
    run_portfolio_walk_forward,
)
from msai.services.portfolio_backtest.safety_caps import SafetyCaps


def test_build_search_space_clips_to_caps() -> None:
    """``suggest_float`` upper bound must equal the safety cap."""
    caps = SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20)
    spec = build_search_space(caps)
    assert spec["leverage"]["high"] == 2.0
    assert spec["position_size"]["high"] == 0.25
    # Lower bounds are sensible
    assert spec["leverage"]["low"] >= 0.0
    assert spec["position_size"]["low"] >= 0.0


def test_build_search_space_handles_unset_position_cap() -> None:
    """When ``max_position_size`` is None, search space falls back to 1.0."""
    caps = SafetyCaps(max_leverage=1.5, max_position_size=None, max_drawdown_halt=None)
    spec = build_search_space(caps)
    assert spec["position_size"]["high"] == 1.0


def test_run_portfolio_walk_forward_invokes_portfolio_backtest_fn() -> None:
    """Smoke — verifies the trial loop calls the injected ``portfolio_backtest_fn``
    and aggregates IS/OOS metrics, without running real Nautilus backtests."""
    calls: list[dict[str, Any]] = []

    def fake_portfolio_backtest_fn(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        # Return varied IS/OOS metrics so aggregation is non-trivial
        is_window = kwargs["start_date"] < date(2024, 7, 1)  # IS = first half
        return {
            "objective": 1.4 if is_window else 1.1,
            "sharpe": 1.4 if is_window else 1.1,
            "total_return": 0.20,
            "max_drawdown": -0.08,
            "total_leverage": 1.5,
            "max_position": 0.15,
        }

    result = run_portfolio_walk_forward(
        portfolio_id="00000000-0000-0000-0000-000000000000",
        member_strategy_ids=["s1", "s2"],
        allocator_name="equal_weight",
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        safety_caps=SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=100_000.0,
        train_days=126,
        test_days=63,
        step_days=63,
        n_trials=4,  # ~1 trial per window
        progress_callback=None,
        cancel_check=lambda: False,
        portfolio_backtest_fn=fake_portfolio_backtest_fn,
    )
    assert isinstance(result, PortfolioOptimizationResult)
    # Every trial calls portfolio_backtest_fn twice (IS + OOS)
    assert len(calls) >= 2
    # Trace records at least one trial
    assert len(result.optimization_trace) >= 1
    # IS-OOS gap is computed
    assert result.generalization_gap is not None


def test_run_portfolio_walk_forward_records_pruned_safety_breach() -> None:
    """Trials whose IS metrics breach safety caps are pruned and recorded."""
    call_count = {"n": 0}

    def fake_fn(**kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        # Return metrics that violate the leverage cap (max_leverage=2.0; here we
        # return total_leverage=10.0).
        return {
            "sharpe": 1.0,
            "total_return": 0.1,
            "max_drawdown": -0.05,
            "total_leverage": 10.0,
            "max_position": 0.05,
        }

    result = run_portfolio_walk_forward(
        portfolio_id="11111111-1111-1111-1111-111111111111",
        member_strategy_ids=["s1"],
        allocator_name="equal_weight",
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        safety_caps=SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=100_000.0,
        train_days=126,
        test_days=63,
        step_days=63,
        n_trials=2,
        portfolio_backtest_fn=fake_fn,
    )
    # The fake_fn was invoked at least once; trace shows pruned trials
    assert call_count["n"] >= 1
    pruned = [row for row in result.optimization_trace if row.get("pruned") == "safety_cap"]
    assert len(pruned) >= 1
    # All trials pruned → IS/OOS scores empty → averages == 0.0
    assert result.is_metric == 0.0
    assert result.oos_metric == 0.0


def test_optimizer_continues_after_trial_exception() -> None:
    """Phase 5.1 P1-C — an exception in ONE trial must not stall the sweep.

    The Optuna ``ask`` / ``tell`` contract requires every ask to be paired
    with a tell; without the try/finally guard in the trial body, an
    exception escaping after ``ask`` (but before ``tell``) would leave the
    study with a phantom RUNNING trial in its journal and block subsequent
    asks. This test forces one trial to raise and asserts:

    1. The trace records both successful trials AND the error row.
    2. Subsequent trials still run (call_count keeps incrementing).
    """
    call_count = {"n": 0}

    def fake_fn(**kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        # Raise on the second IS evaluation, succeed on the others.
        if call_count["n"] == 3:
            raise RuntimeError("synthetic mid-trial failure")
        return {
            "sharpe": 1.0,
            "total_return": 0.1,
            "max_drawdown": -0.05,
            "total_leverage": 1.2,
            "max_position": 0.15,
        }

    result = run_portfolio_walk_forward(
        portfolio_id="33333333-3333-3333-3333-333333333333",
        member_strategy_ids=["s1"],
        allocator_name="equal_weight",
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        safety_caps=SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=100_000.0,
        train_days=126,
        test_days=63,
        step_days=63,
        n_trials=4,
        portfolio_backtest_fn=fake_fn,
    )

    # Trace must contain at least one error row.
    error_rows = [row for row in result.optimization_trace if "error" in row]
    assert error_rows, "trial exception must be recorded in the trace"
    assert any("synthetic mid-trial failure" in row.get("error", "") for row in error_rows)

    # Successful trials still ran after the exception — call_count keeps
    # incrementing past the raising call.
    assert call_count["n"] > 3, (
        f"sweep must continue past the failed trial; saw {call_count['n']} calls"
    )

    # Trace also has at least one successful trial (with is_score / oos_score).
    success_rows = [row for row in result.optimization_trace if "is_score" in row]
    assert success_rows, "non-failing trials must produce score rows"


def test_run_portfolio_walk_forward_respects_cancel_check() -> None:
    """When ``cancel_check`` returns True, the loop exits cleanly without calls."""
    calls: list[dict[str, Any]] = []

    def fake_fn(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"sharpe": 1.0, "total_return": 0.1, "max_drawdown": -0.05}

    result = run_portfolio_walk_forward(
        portfolio_id="22222222-2222-2222-2222-222222222222",
        member_strategy_ids=["s1"],
        allocator_name="equal_weight",
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        safety_caps=SafetyCaps(max_leverage=2.0),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=100_000.0,
        train_days=126,
        test_days=63,
        step_days=63,
        n_trials=4,
        cancel_check=lambda: True,  # cancel immediately
        portfolio_backtest_fn=fake_fn,
    )
    assert len(calls) == 0
    assert isinstance(result, PortfolioOptimizationResult)


def test_run_portfolio_walk_forward_caps_total_trials_at_n_trials() -> None:
    """Codex bot iter-8 P2 on PR #73 — when ``total_windows > n_trials``,
    the optimizer must run EXACTLY ``n_trials`` total, not one per
    window. The previous ``max(1, n_trials // total_windows)`` formula
    ran at least one trial in every window, blowing through small
    explicit caps (e.g., n_trials=2 with 8 windows ran 8 trials).
    """
    fn_calls: list[dict[str, Any]] = []

    def fake_fn(**kwargs: Any) -> dict[str, Any]:
        fn_calls.append(kwargs)
        return {
            "sharpe": 1.0,
            "total_return": 0.1,
            "max_drawdown": -0.05,
            "total_leverage": 1.0,
            "max_position": 0.05,
        }

    # 365-day range with 21-day train / 7-day test / 7-day step
    # → ~50 walk-forward windows. n_trials=3 must produce 3 total trials,
    # NOT 50.
    result = run_portfolio_walk_forward(
        portfolio_id="22222222-2222-2222-2222-222222222222",
        member_strategy_ids=["s1"],
        allocator_name="equal_weight",
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        safety_caps=SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=100_000.0,
        train_days=21,
        test_days=7,
        step_days=7,
        n_trials=3,
        portfolio_backtest_fn=fake_fn,
    )
    # Trace rows = one per (trial, window). With n_trials=3 distributed
    # across ~50 windows, exactly 3 windows get 1 trial each → trace
    # length is 3.
    trial_rows = [r for r in result.optimization_trace if "trial" in r]
    # Group by trial number to get unique trials.
    unique_trials = {r["trial"] for r in trial_rows}
    assert len(unique_trials) == 3, (
        f"n_trials=3 must produce exactly 3 unique trials; got "
        f"{len(unique_trials)} (trace has {len(trial_rows)} rows)"
    )
    # And the optimizer's portfolio_backtest_fn was called for each trial's
    # IS + OOS evaluation. 3 trials × 2 calls per trial = 6 calls.
    # (Pruned-on-IS trials would skip the OOS call, but our fake_fn returns
    # within caps so neither IS nor OOS is pruned.)
    assert len(fn_calls) == 6, (
        f"expected 6 backtest_fn calls (3 trials × IS+OOS); got {len(fn_calls)}"
    )


def test_run_portfolio_walk_forward_pairs_is_oos_appends() -> None:
    """Ultrareview bug_003 on PR #73 — if the OOS evaluation raises mid-
    trial, the IS append must NOT leave an orphan entry that drifts
    the array lengths. Before the fix, ``in_sample_scores.append`` ran
    BEFORE OOS evaluation; OOS failures left ``len(in_sample_scores) >
    len(out_of_sample_scores)`` which biased the averaging at the end.

    Forces the first trial's OOS to raise, asserts the resulting trace
    shows the FAIL row and the surviving IS/OOS data come from a
    matched pair.
    """
    call_count = {"n": 0}

    def fake_fn_oos_raises_on_first_trial(**kwargs: Any) -> dict[str, Any]:
        call_count["n"] += 1
        # Calls 1, 3 (IS for trial 0 + trial 1), Calls 2, 4 (OOS for trial 0 + trial 1).
        # Make the OOS call for trial 0 (call #2) raise.
        if call_count["n"] == 2:
            raise RuntimeError("simulated OOS failure")
        return {
            "sharpe": 1.0,
            "total_return": 0.1,
            "max_drawdown": -0.05,
            "total_leverage": 1.0,
            "max_position": 0.05,
        }

    result = run_portfolio_walk_forward(
        portfolio_id="33333333-3333-3333-3333-333333333333",
        member_strategy_ids=["s1"],
        allocator_name="equal_weight",
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        safety_caps=SafetyCaps(max_leverage=2.0, max_position_size=0.25, max_drawdown_halt=0.20),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        initial_capital=100_000.0,
        train_days=126,
        test_days=63,
        step_days=63,
        n_trials=2,
        portfolio_backtest_fn=fake_fn_oos_raises_on_first_trial,
    )

    # Trace contains a FAIL row for trial 0 (OOS raised) and a normal
    # row for trial 1 (both halves succeeded).
    error_rows = [r for r in result.optimization_trace if "error" in r]
    assert len(error_rows) == 1, (
        f"expected 1 error row for the failed OOS trial; got {len(error_rows)}"
    )

    # The load-bearing assertion: IS/OOS averages are now computed over
    # the SAME population because the appends are paired. If the bug
    # were still present, is_metric / oos_metric would be derived from
    # divergent denominators. Both should be ~1.0 (the fake fn's score)
    # for the one trial that fully succeeded.
    assert result.is_metric == pytest.approx(1.0), (
        f"is_metric must come from same-trial population as oos_metric; got {result.is_metric}"
    )
    assert result.oos_metric == pytest.approx(1.0), (
        f"oos_metric must come from same-trial population as is_metric; got {result.oos_metric}"
    )


def test_slice_cached_returns_includes_boundary_day_intraday() -> None:
    """Ultrareview bug_008 on PR #73 — ``_slice_cached_returns`` used
    inclusive bounds on midnight-UTC timestamps while the cache is
    minute-granular, so every intraday bar on the boundary day was
    excluded from BOTH train and test slices. The fix shifts the upper
    bound to end-of-day so the boundary day's intraday bars land in
    exactly one window.
    """
    from msai.services.portfolio.orchestration import _slice_cached_returns

    # 5 intraday bars on the boundary day (2024-03-31), each at 09:30..09:34.
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2024-03-31 09:30:00+00:00"),
            pd.Timestamp("2024-03-31 09:31:00+00:00"),
            pd.Timestamp("2024-03-31 09:32:00+00:00"),
            pd.Timestamp("2024-04-01 09:30:00+00:00"),
            pd.Timestamp("2024-04-01 09:31:00+00:00"),
        ]
    )
    series = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05], index=idx)

    # Train window ending 2024-03-31 (midnight UTC).
    train_start = pd.Timestamp("2024-03-01 00:00:00+00:00")
    train_end = pd.Timestamp("2024-03-31 00:00:00+00:00")
    train_slice = _slice_cached_returns(series, train_start, train_end)
    # Boundary day's intraday bars MUST be in the train slice.
    assert len(train_slice) == 3, (
        f"train slice must include all 03-31 intraday bars; got {len(train_slice)}"
    )

    # Test window starting 2024-04-01.
    test_start = pd.Timestamp("2024-04-01 00:00:00+00:00")
    test_end = pd.Timestamp("2024-04-01 00:00:00+00:00")
    test_slice = _slice_cached_returns(series, test_start, test_end)
    # All 04-01 intraday bars land in test, not train.
    assert len(test_slice) == 2, (
        f"test slice must include all 04-01 intraday bars; got {len(test_slice)}"
    )

    # Total preserved with no duplication.
    assert len(train_slice) + len(test_slice) == 5
