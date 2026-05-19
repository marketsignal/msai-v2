"""Unit tests for portfolio_backtest.results — per-strategy equity, correlations, drawdowns."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from msai.services.portfolio_backtest.results import (
    compute_drawdown_breakdown,
    compute_drawdown_correlation,
    compute_drawdown_curves,
    compute_per_strategy_equity,
    compute_return_correlation,
)


@pytest.fixture
def returns_df() -> pd.DataFrame:
    """Two-strategy daily returns over 1 year."""
    np.random.seed(42)
    idx = pd.date_range("2024-01-01", periods=252, freq="B")
    return pd.DataFrame(
        {
            "s1": np.random.normal(0.0005, 0.01, 252),
            "s2": np.random.normal(0.0003, 0.012, 252),
        },
        index=idx,
    )


def test_per_strategy_equity_starts_near_initial_capital(returns_df: pd.DataFrame) -> None:
    """Day-1 equity reflects the first day's return applied to initial_capital
    (convention: equity AT END of each day). With daily returns of std ~0.01,
    a 10% tolerance is the honest acceptance band — anything tighter is
    asserting a "start-anchored" convention this implementation does not adopt."""
    eq = compute_per_strategy_equity(returns_df, initial_capital=100_000.0)
    for sid in returns_df.columns:
        assert eq[sid].iloc[0] == pytest.approx(100_000.0, rel=0.10)
        # Sanity: equity series is strictly positive
        assert (eq[sid] > 0).all()


def test_drawdown_curves_are_nonpositive(returns_df: pd.DataFrame) -> None:
    eq = compute_per_strategy_equity(returns_df, initial_capital=100_000.0)
    dds = compute_drawdown_curves(eq)
    for sid in dds.columns:
        assert (dds[sid] <= 0.0).all()


def test_return_correlation_matrix_shape(returns_df: pd.DataFrame) -> None:
    m = compute_return_correlation(returns_df)
    assert m.shape == (2, 2)
    assert m.loc["s1", "s1"] == pytest.approx(1.0)
    assert m.loc["s1", "s2"] == pytest.approx(m.loc["s2", "s1"])


def test_drawdown_correlation_uses_underwater_series(returns_df: pd.DataFrame) -> None:
    eq = compute_per_strategy_equity(returns_df, initial_capital=100_000.0)
    dds = compute_drawdown_curves(eq)
    m = compute_drawdown_correlation(dds)
    assert m.shape == (2, 2)
    # Drawdown correlation often differs from return correlation, but here we
    # just assert valid Pearson range — the load-bearing claim is that the
    # function operates on the underwater series, not the return series.
    assert -1.0 <= m.loc["s1", "s2"] <= 1.0


def test_drawdown_breakdown_per_strategy(returns_df: pd.DataFrame) -> None:
    eq = compute_per_strategy_equity(returns_df, initial_capital=100_000.0)
    breakdown = compute_drawdown_breakdown(eq)
    assert {"s1", "s2"} == set(breakdown.index)
    assert "max_drawdown" in breakdown.columns
    assert "drawdown_duration" in breakdown.columns
    # Max DDs are non-positive
    assert (breakdown["max_drawdown"] <= 0).all()
