"""Unit tests for the research engine — parameter sweeps, walk-forward, and helpers."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from msai.services.research_engine import (
    ResearchEngine,
    build_walk_forward_windows,
    expand_parameter_grid,
    extract_objective_value,
    rank_results,
)


# ---------------------------------------------------------------------------
# expand_parameter_grid
# ---------------------------------------------------------------------------


class TestExpandParameterGrid:
    def test_two_params_generates_all_combinations(self) -> None:
        grid = {"fast_period": [5, 10], "slow_period": [20, 50]}
        result = expand_parameter_grid(grid)
        assert len(result) == 4
        assert {"fast_period": 5, "slow_period": 20} in result
        assert {"fast_period": 5, "slow_period": 50} in result
        assert {"fast_period": 10, "slow_period": 20} in result
        assert {"fast_period": 10, "slow_period": 50} in result

    def test_three_params_generates_cartesian_product(self) -> None:
        grid = {"a": [1, 2], "b": [10], "c": ["x", "y", "z"]}
        result = expand_parameter_grid(grid)
        assert len(result) == 2 * 1 * 3  # 6 combinations

    def test_empty_grid_returns_single_empty_dict(self) -> None:
        result = expand_parameter_grid({})
        assert result == [{}]

    def test_empty_values_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            expand_parameter_grid({"a": []})

    def test_single_param_single_value(self) -> None:
        result = expand_parameter_grid({"x": [42]})
        assert result == [{"x": 42}]


# ---------------------------------------------------------------------------
# rank_results
# ---------------------------------------------------------------------------


class TestRankResults:
    def test_sorts_by_sharpe_descending(self) -> None:
        results = [
            _result(sharpe_ratio=1.5, completed=True),
            _result(sharpe_ratio=2.5, completed=True),
            _result(sharpe_ratio=0.5, completed=True),
        ]
        ranked = rank_results(results, objective="sharpe")
        sharpes = [r["metrics"]["sharpe_ratio"] for r in ranked]
        assert sharpes == [2.5, 1.5, 0.5]

    def test_errors_ranked_last(self) -> None:
        results = [
            _result(sharpe_ratio=0.1, completed=True),
            {"error": "boom", "metrics": None, "pruned": False},
            _result(sharpe_ratio=3.0, completed=True),
        ]
        ranked = rank_results(results, objective="sharpe")
        assert ranked[-1].get("error") == "boom"

    def test_pruned_ranked_below_completed(self) -> None:
        results = [
            _result(sharpe_ratio=1.0, completed=True),
            _result(sharpe_ratio=5.0, completed=False, pruned=True),
        ]
        ranked = rank_results(results, objective="sharpe")
        # The completed result should come first even though pruned has higher sharpe
        assert ranked[0]["completed_full_run"] is True

    def test_empty_results_returns_empty(self) -> None:
        assert rank_results([], objective="sharpe") == []

    def test_holdout_validated_preferred_over_train_only(self) -> None:
        results = [
            _result(sharpe_ratio=3.0, completed=True, selection_basis="train"),
            _result(sharpe_ratio=2.0, completed=True, selection_basis="holdout"),
        ]
        ranked = rank_results(results, objective="sharpe")
        # Holdout-validated comes first even with lower sharpe
        assert ranked[0]["selection_basis"] == "holdout"


# ---------------------------------------------------------------------------
# build_walk_forward_windows
# ---------------------------------------------------------------------------


class TestBuildWalkForwardWindows:
    def test_rolling_windows_with_known_dates(self) -> None:
        windows = build_walk_forward_windows(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 6, 30),
            train_days=90,
            test_days=30,
            mode="rolling",
        )
        assert len(windows) >= 1
        first = windows[0]
        assert first["train_start"] == date(2024, 1, 1)
        assert first["train_end"] == date(2024, 3, 30)  # Jan 1 + 89 days
        assert first["test_start"] == date(2024, 3, 31)
        assert first["test_end"] == date(2024, 4, 29)

    def test_expanding_windows_have_fixed_start(self) -> None:
        windows = build_walk_forward_windows(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            train_days=90,
            test_days=30,
            step_days=30,
            mode="expanding",
        )
        # All windows should start training from the same date
        for w in windows:
            assert w["train_start"] == date(2024, 1, 1)

    def test_step_days_defaults_to_test_days(self) -> None:
        windows_default = build_walk_forward_windows(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            train_days=90,
            test_days=30,
        )
        windows_explicit = build_walk_forward_windows(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
            train_days=90,
            test_days=30,
            step_days=30,
        )
        assert len(windows_default) == len(windows_explicit)

    def test_no_windows_fit_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="No walk-forward windows"):
            build_walk_forward_windows(
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 10),
                train_days=90,
                test_days=30,
            )

    def test_invalid_mode_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="mode must be"):
            build_walk_forward_windows(
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
                train_days=90,
                test_days=30,
                mode="invalid",
            )

    def test_negative_train_days_raises(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            build_walk_forward_windows(
                start_date=date(2024, 1, 1),
                end_date=date(2024, 12, 31),
                train_days=-1,
                test_days=30,
            )


# ---------------------------------------------------------------------------
# extract_objective_value
# ---------------------------------------------------------------------------


class TestExtractObjectiveValue:
    def test_sharpe_short_name(self) -> None:
        metrics = {"sharpe_ratio": 1.8, "sortino_ratio": 2.1}
        assert extract_objective_value(metrics, "sharpe") == 1.8

    def test_sortino_short_name(self) -> None:
        metrics = {"sortino_ratio": 2.5}
        assert extract_objective_value(metrics, "sortino") == 2.5

    def test_total_return(self) -> None:
        metrics = {"total_return": 0.15}
        assert extract_objective_value(metrics, "total_return") == 0.15

    def test_max_drawdown_negated(self) -> None:
        # max_drawdown of -0.1 should be negated to 0.1 for maximization
        metrics = {"max_drawdown": -0.1}
        assert extract_objective_value(metrics, "max_drawdown") == -0.1  # -abs(-0.1)

    def test_missing_metric_returns_zero(self) -> None:
        assert extract_objective_value({}, "sharpe") == 0.0

    def test_nan_returns_zero(self) -> None:
        assert extract_objective_value({"sharpe_ratio": float("nan")}, "sharpe") == 0.0

    def test_full_key_name_works(self) -> None:
        metrics = {"sharpe_ratio": 1.2}
        assert extract_objective_value(metrics, "sharpe_ratio") == 1.2


# ---------------------------------------------------------------------------
# ResearchEngine.__init__
# ---------------------------------------------------------------------------


class TestResearchEngineInit:
    def test_creates_default_runner(self) -> None:
        engine = ResearchEngine()
        assert engine.runner is not None

    def test_accepts_custom_runner(self) -> None:
        mock_runner = MagicMock()
        engine = ResearchEngine(runner=mock_runner)
        assert engine.runner is mock_runner


# ---------------------------------------------------------------------------
# Integration: simple 2-param sweep with mock runner
# ---------------------------------------------------------------------------


class TestParameterSweepWithMockRunner:
    def test_simple_grid_sweep_returns_results(self) -> None:
        mock_runner = MagicMock()
        mock_runner.run.return_value = _mock_backtest_result(
            sharpe_ratio=1.5, total_return=0.1
        )

        engine = ResearchEngine(runner=mock_runner)
        result = engine.run_parameter_sweep(
            strategy_path="/fake/strategy.py",
            base_config={"instrument_id": "AAPL.SIM"},
            parameter_grid={"fast_period": [5, 10]},
            instruments=["AAPL.SIM"],
            start_date="2024-01-01",
            end_date="2024-03-31",
            data_path=Path("/fake/data"),
            objective="sharpe",
            search_strategy="grid",
        )

        assert result["mode"] == "parameter_sweep"
        assert result["objective"] == "sharpe"
        assert len(result["results"]) == 2
        assert result["summary"]["total_runs"] == 2
        assert result["summary"]["successful_runs"] == 2
        assert result["summary"]["best_result"] is not None

    def test_sweep_calls_runner_for_each_combination(self) -> None:
        mock_runner = MagicMock()
        mock_runner.run.return_value = _mock_backtest_result(sharpe_ratio=1.0)

        engine = ResearchEngine(runner=mock_runner)
        engine.run_parameter_sweep(
            strategy_path="/fake/strategy.py",
            base_config={},
            parameter_grid={"a": [1, 2], "b": [10, 20]},
            instruments=["AAPL.SIM"],
            start_date="2024-01-01",
            end_date="2024-03-31",
            data_path=Path("/fake/data"),
            search_strategy="grid",
        )

        # 4 combinations should produce 4 runner calls
        assert mock_runner.run.call_count == 4

    def test_sweep_handles_runner_errors(self) -> None:
        mock_runner = MagicMock()
        mock_runner.run.side_effect = RuntimeError("backtest exploded")

        engine = ResearchEngine(runner=mock_runner)
        result = engine.run_parameter_sweep(
            strategy_path="/fake/strategy.py",
            base_config={},
            parameter_grid={"x": [1]},
            instruments=["AAPL.SIM"],
            start_date="2024-01-01",
            end_date="2024-03-31",
            data_path=Path("/fake/data"),
            search_strategy="grid",
        )

        assert result["summary"]["successful_runs"] == 0
        assert result["results"][0]["error"] == "backtest exploded"

    def test_progress_callback_called(self) -> None:
        mock_runner = MagicMock()
        mock_runner.run.return_value = _mock_backtest_result(sharpe_ratio=1.0)
        callback = MagicMock()

        engine = ResearchEngine(runner=mock_runner)
        engine.run_parameter_sweep(
            strategy_path="/fake/strategy.py",
            base_config={},
            parameter_grid={"x": [1]},
            instruments=["AAPL.SIM"],
            start_date="2024-01-01",
            end_date="2024-03-31",
            data_path=Path("/fake/data"),
            search_strategy="grid",
            progress_callback=callback,
        )

        assert callback.call_count >= 1
        last_call = callback.call_args_list[-1]
        assert "progress" in last_call[0][0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(
    *,
    sharpe_ratio: float = 0.0,
    completed: bool = True,
    pruned: bool = False,
    selection_basis: str = "train",
) -> dict[str, Any]:
    """Build a minimal result dict for ranking tests."""
    return {
        "config": {"fast_period": 10},
        "error": None,
        "metrics": {"sharpe_ratio": sharpe_ratio, "total_return": 0.05},
        "pruned": pruned,
        "completed_full_run": completed,
        "selection_basis": selection_basis,
    }


def _mock_backtest_result(
    sharpe_ratio: float = 0.0,
    total_return: float = 0.0,
) -> MagicMock:
    """Build a mock BacktestResult with the expected metrics dict."""
    result = MagicMock()
    result.metrics = {
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": 0.0,
        "max_drawdown": 0.0,
        "total_return": total_return,
        "win_rate": 0.0,
        "num_trades": 10,
    }
    return result
