"""Post-run results computation: per-strategy equity, correlations, drawdowns.

All functions are pure pandas; no Nautilus dependency. They accept a
returns DataFrame (columns = strategy_id, rows = dates) and produce derived
analytics for the results page.
"""

from __future__ import annotations

import pandas as pd


def compute_per_strategy_equity(returns: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    """Compound each strategy's daily returns into an equity curve.

    Equity_t = initial_capital * cumprod(1 + return_t).
    """
    return (1.0 + returns).cumprod() * initial_capital


def compute_drawdown_curves(equity: pd.DataFrame) -> pd.DataFrame:
    """Per-strategy underwater (drawdown) series.

    DD_t = equity_t / running_max(equity_t) - 1.0
    Returns a non-positive series; 0 at peaks, more negative in troughs.
    """
    return equity / equity.cummax() - 1.0


def compute_return_correlation(returns: pd.DataFrame) -> pd.DataFrame:
    """Pearson correlation matrix of strategy return series."""
    return returns.corr(method="pearson")


def compute_drawdown_correlation(drawdowns: pd.DataFrame) -> pd.DataFrame:
    """Pearson correlation matrix of strategy drawdown (underwater) series.

    Drawdown correlation measures real-diversification potential — return
    correlation can look favorable while two strategies draw down at the same
    times. Reference: docs/research/2026-05-18-portfolio-backtest.md § 6.
    """
    return drawdowns.corr(method="pearson")


def compute_drawdown_breakdown(equity: pd.DataFrame) -> pd.DataFrame:
    """Per-strategy max drawdown + drawdown duration.

    Returns a DataFrame with columns ``max_drawdown`` and ``drawdown_duration``
    (number of business days from peak to recovery, or to end-of-period if not
    recovered), indexed by strategy_id.
    """
    rows: list[dict[str, object]] = []
    for sid in equity.columns:
        eq = equity[sid]
        running_max = eq.cummax()
        dd = eq / running_max - 1.0
        max_dd = float(dd.min())
        # Drawdown duration: longest gap between peaks
        trough_idx = dd.idxmin()
        # Find when running_max first hit the peak preceding the trough
        peak_idx = running_max.loc[:trough_idx].idxmax()
        # Find recovery (or end)
        post_trough = eq.loc[trough_idx:]
        peak_val = running_max.loc[trough_idx]
        recovered = post_trough[post_trough >= peak_val]
        end_idx = recovered.index[0] if len(recovered) > 0 else eq.index[-1]
        duration_days = int((end_idx - peak_idx).days)
        rows.append(
            {
                "strategy_id": sid,
                "max_drawdown": max_dd,
                "drawdown_duration": duration_days,
            }
        )
    return pd.DataFrame(rows).set_index("strategy_id")
