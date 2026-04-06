"""Unit tests for msai.services.nautilus.backtest_runner module."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from msai.services.nautilus.backtest_runner import BacktestResult, BacktestRunner
from msai.services.strategy_registry import load_strategy_class

_STRATEGY_PATH = Path(__file__).resolve().parents[3] / "strategies" / "example" / "ema_cross.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_ema_cross_class() -> type:
    """Load EMACrossStrategy from the strategies directory."""
    return load_strategy_class(_STRATEGY_PATH, "EMACrossStrategy")


def _sample_trending_bars(n: int = 100) -> pd.DataFrame:
    """Generate bars with a rising then falling trend to trigger crossovers.

    The price rises for the first half and falls for the second half,
    guaranteeing at least one buy and one sell signal.
    """
    timestamps = pd.date_range("2024-01-02 09:30", periods=n, freq="1min")
    half = n // 2

    prices: list[float] = []
    # Rising phase
    for i in range(half):
        prices.append(100.0 + i * 0.5)
    # Falling phase
    peak = prices[-1]
    for i in range(n - half):
        prices.append(peak - i * 0.8)

    return pd.DataFrame(
        {
            "symbol": ["AAPL"] * n,
            "timestamp": timestamps,
            "open": [p - 0.1 for p in prices],
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": prices,
            "volume": [10000] * n,
        }
    )


def _sample_flat_bars(n: int = 100) -> pd.DataFrame:
    """Generate bars with constant price (no crossovers possible)."""
    timestamps = pd.date_range("2024-01-02 09:30", periods=n, freq="1min")
    return pd.DataFrame(
        {
            "symbol": ["AAPL"] * n,
            "timestamp": timestamps,
            "open": [100.0] * n,
            "high": [100.5] * n,
            "low": [99.5] * n,
            "close": [100.0] * n,
            "volume": [10000] * n,
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBacktestRunnerRun:
    """Tests for BacktestRunner.run."""

    def test_run_returns_backtest_result(self) -> None:
        """Run with trending data returns a valid BacktestResult."""
        # Arrange
        runner = BacktestRunner()
        strategy_cls = _load_ema_cross_class()
        bars = _sample_trending_bars(200)

        # Act
        result = runner.run(
            strategy_class=strategy_cls,
            config={"fast_period": 5, "slow_period": 15, "trade_size": 10.0},
            bars_df=bars,
            initial_cash=100_000.0,
        )

        # Assert
        assert isinstance(result, BacktestResult)
        assert isinstance(result.orders_df, pd.DataFrame)
        assert isinstance(result.positions_df, pd.DataFrame)
        assert isinstance(result.account_df, pd.DataFrame)
        assert isinstance(result.metrics, dict)
        assert isinstance(result.returns_series, pd.Series)
        assert len(result.account_df) == len(bars)
        assert len(result.positions_df) == len(bars)

    def test_run_calculates_metrics(self) -> None:
        """Run produces metrics with required keys: sharpe, drawdown, return."""
        # Arrange
        runner = BacktestRunner()
        strategy_cls = _load_ema_cross_class()
        bars = _sample_trending_bars(200)

        # Act
        result = runner.run(
            strategy_class=strategy_cls,
            config={"fast_period": 5, "slow_period": 15, "trade_size": 10.0},
            bars_df=bars,
            initial_cash=100_000.0,
        )

        # Assert -- all expected metric keys are present
        assert "total_return" in result.metrics
        assert "sharpe_ratio" in result.metrics
        assert "sortino_ratio" in result.metrics
        assert "max_drawdown" in result.metrics
        assert "win_rate" in result.metrics
        assert "num_trades" in result.metrics
        assert "initial_cash" in result.metrics
        assert "final_equity" in result.metrics

        # Metrics should be numeric
        assert isinstance(result.metrics["total_return"], float)
        assert isinstance(result.metrics["sharpe_ratio"], float)
        assert isinstance(result.metrics["max_drawdown"], float)

    def test_run_generates_trades(self) -> None:
        """Run with trending data should produce at least one trade."""
        # Arrange
        runner = BacktestRunner()
        strategy_cls = _load_ema_cross_class()
        bars = _sample_trending_bars(200)

        # Act
        result = runner.run(
            strategy_class=strategy_cls,
            config={"fast_period": 5, "slow_period": 15, "trade_size": 10.0},
            bars_df=bars,
        )

        # Assert
        assert result.metrics["num_trades"] >= 1
        assert len(result.orders_df) >= 1
        assert "side" in result.orders_df.columns
        assert "price" in result.orders_df.columns
        assert "quantity" in result.orders_df.columns

    def test_run_with_no_trades(self) -> None:
        """Run with flat prices generates no trades; metrics handled gracefully."""
        # Arrange
        runner = BacktestRunner()
        strategy_cls = _load_ema_cross_class()
        bars = _sample_flat_bars(100)

        # Act
        result = runner.run(
            strategy_class=strategy_cls,
            config={"fast_period": 5, "slow_period": 15, "trade_size": 10.0},
            bars_df=bars,
            initial_cash=50_000.0,
        )

        # Assert
        assert result.metrics["num_trades"] == 0
        assert result.metrics["total_return"] == 0.0
        assert result.metrics["final_equity"] == 50_000.0
        assert result.metrics["max_drawdown"] == 0.0
        assert result.orders_df.empty

    def test_run_equity_starts_at_initial_cash(self) -> None:
        """Equity curve starts at the initial cash value."""
        # Arrange
        runner = BacktestRunner()
        strategy_cls = _load_ema_cross_class()
        bars = _sample_flat_bars(50)

        # Act
        result = runner.run(
            strategy_class=strategy_cls,
            config={"fast_period": 5, "slow_period": 15},
            bars_df=bars,
            initial_cash=75_000.0,
        )

        # Assert
        assert result.account_df["equity"].iloc[0] == 75_000.0

    def test_run_with_empty_bars(self) -> None:
        """Run with empty DataFrame returns empty result gracefully."""
        # Arrange
        runner = BacktestRunner()
        strategy_cls = _load_ema_cross_class()
        bars = pd.DataFrame()

        # Act
        result = runner.run(
            strategy_class=strategy_cls,
            config={"fast_period": 5, "slow_period": 15},
            bars_df=bars,
        )

        # Assert
        assert result.orders_df.empty
        assert result.account_df.empty
        assert result.metrics["num_trades"] == 0
        assert result.metrics["total_return"] == 0.0

    def test_run_max_drawdown_is_non_positive(self) -> None:
        """Max drawdown should be zero or negative."""
        # Arrange
        runner = BacktestRunner()
        strategy_cls = _load_ema_cross_class()
        bars = _sample_trending_bars(200)

        # Act
        result = runner.run(
            strategy_class=strategy_cls,
            config={"fast_period": 5, "slow_period": 15, "trade_size": 10.0},
            bars_df=bars,
        )

        # Assert
        assert result.metrics["max_drawdown"] <= 0.0
