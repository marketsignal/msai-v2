from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class SeriesMetrics:
    total_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    win_rate: float
    annualized_volatility: float
    downside_risk: float
    alpha: float | None
    beta: float | None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "total_return": self.total_return,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "annualized_volatility": self.annualized_volatility,
            "downside_risk": self.downside_risk,
            "alpha": self.alpha,
            "beta": self.beta,
        }


def build_series_from_returns(
    returns: pd.Series,
    *,
    base_value: float = 1.0,
) -> pd.DataFrame:
    series = _clean_returns_series(returns)
    if series.empty:
        return pd.DataFrame(columns=["timestamp", "returns", "equity", "drawdown"])

    equity = (1.0 + series).cumprod() * float(base_value)
    drawdown = equity / equity.cummax() - 1.0
    frame = pd.DataFrame(
        {
            "timestamp": series.index,
            "returns": series.values,
            "equity": equity.values,
            "drawdown": drawdown.values,
        }
    )
    return frame


def compute_series_metrics(
    returns: pd.Series,
    *,
    benchmark_returns: pd.Series | None = None,
) -> SeriesMetrics:
    series = _clean_returns_series(returns)
    if series.empty:
        return SeriesMetrics(
            total_return=0.0,
            sharpe=0.0,
            sortino=0.0,
            max_drawdown=0.0,
            win_rate=0.0,
            annualized_volatility=0.0,
            downside_risk=0.0,
            alpha=None,
            beta=None,
        )

    periods = infer_periods_per_year(series.index)
    mean_return = float(series.mean())
    std_return = float(series.std(ddof=0))
    downside = series.where(series < 0.0, 0.0)
    downside_std = float((downside.pow(2).mean()) ** 0.5)
    equity = (1.0 + series).cumprod()
    drawdown = equity / equity.cummax() - 1.0

    alpha: float | None = None
    beta: float | None = None
    if benchmark_returns is not None:
        alpha, beta = compute_alpha_beta(series, benchmark_returns, periods_per_year=periods)

    return SeriesMetrics(
        total_return=float(equity.iloc[-1] - 1.0),
        sharpe=_safe_ratio(mean_return, std_return) * math.sqrt(periods),
        sortino=_safe_ratio(mean_return, downside_std) * math.sqrt(periods),
        max_drawdown=float(drawdown.min()),
        win_rate=float((series > 0.0).mean()),
        annualized_volatility=float(std_return * math.sqrt(periods)),
        downside_risk=float(downside_std * math.sqrt(periods)),
        alpha=alpha,
        beta=beta,
    )


def compute_alpha_beta(
    returns: pd.Series,
    benchmark_returns: pd.Series,
    *,
    periods_per_year: float | None = None,
) -> tuple[float | None, float | None]:
    aligned = pd.concat(
        [
            _clean_returns_series(returns).rename("strategy"),
            _clean_returns_series(benchmark_returns).rename("benchmark"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    if aligned.empty:
        return None, None

    benchmark_var = float(aligned["benchmark"].var(ddof=0))
    if benchmark_var <= 0:
        return None, None

    beta = float(aligned["strategy"].cov(aligned["benchmark"])) / benchmark_var
    periods = periods_per_year or infer_periods_per_year(aligned.index)
    alpha = float((aligned["strategy"].mean() - beta * aligned["benchmark"].mean()) * periods)
    return alpha, beta


def combine_weighted_returns(
    weighted_series: list[tuple[str, float, pd.Series]],
    *,
    leverage: float = 1.0,
) -> pd.Series:
    if not weighted_series:
        return pd.Series(dtype=float)

    frame = pd.concat(
        [(_clean_returns_series(series) * weight).rename(name) for name, weight, series in weighted_series],
        axis=1,
        join="outer",
    ).sort_index()
    frame = frame.fillna(0.0)
    combined = frame.sum(axis=1) * float(leverage)
    combined.name = "portfolio_returns"
    return combined


def normalize_weights(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = sum(max(float(row.get("weight", 0.0) or 0.0), 0.0) for row in rows)
    if total <= 0:
        equal = 1.0 / len(rows) if rows else 0.0
        return [{**row, "weight": equal} for row in rows]
    return [{**row, "weight": max(float(row.get("weight", 0.0) or 0.0), 0.0) / total} for row in rows]


def infer_periods_per_year(index: pd.Index) -> float:
    if len(index) < 2:
        return 252.0

    try:
        dt_index = pd.to_datetime(index, utc=True)
    except Exception:
        return 252.0

    deltas = dt_index.to_series().diff().dropna()
    if deltas.empty:
        return 252.0

    median_seconds = float(deltas.dt.total_seconds().median())
    if median_seconds <= 90:
        return 252.0 * 390.0
    if median_seconds <= 60.0 * 15.0:
        return 252.0 * 26.0
    if median_seconds <= 60.0 * 90.0:
        return 252.0 * 6.5
    if median_seconds <= 60.0 * 60.0 * 12.0:
        return 252.0
    return 252.0


def dataframe_to_series_payload(frame: pd.DataFrame) -> list[dict[str, float | str]]:
    if frame.empty:
        return []
    payload: list[dict[str, float | str]] = []
    for row in frame.itertuples(index=False):
        payload.append(
            {
                "timestamp": pd.Timestamp(row.timestamp).isoformat(),
                "returns": float(getattr(row, "returns", 0.0)),
                "equity": float(getattr(row, "equity", 0.0)),
                "drawdown": float(getattr(row, "drawdown", 0.0)),
            }
        )
    return payload


def _clean_returns_series(series: pd.Series) -> pd.Series:
    if series.empty:
        return pd.Series(dtype=float)
    cleaned = pd.to_numeric(series, errors="coerce").replace([float("inf"), float("-inf")], pd.NA).dropna()
    if not isinstance(cleaned.index, pd.DatetimeIndex):
        cleaned.index = pd.to_datetime(cleaned.index, utc=True, errors="coerce")
    cleaned = cleaned[~cleaned.index.isna()]  # type: ignore[attr-defined]
    return cleaned.astype(float).sort_index()


def _safe_ratio(numerator: float, denominator: float) -> float:
    if abs(denominator) <= 1e-12:
        return 0.0
    return numerator / denominator
