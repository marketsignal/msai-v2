from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, TypedDict

import pandas as pd
from pandas.api.types import is_numeric_dtype


class _DailyPointDict(TypedDict):
    date: str
    equity: float
    drawdown: float
    daily_return: float


class _MonthlyReturnDict(TypedDict):
    month: str
    pct: float


class _PayloadDict(TypedDict):
    daily: list[_DailyPointDict]
    monthly_returns: list[_MonthlyReturnDict]


def normalize_daily_returns(series: pd.Series | None) -> pd.Series:
    """Canonical returns normalization shared by the QuantStats report
    generator and the persisted ``Backtest.series`` payload.

    QuantStats treats every row as one trading period and annualises with
    ``sqrt(252)``, so minute-bar input inflates Sharpe/Sortino/vol by
    ``sqrt(390)`` (~20x).  We group by UTC calendar date and compound each
    day's returns back to a single daily observation.  Already-daily input
    round-trips unchanged because ``(1 + r).prod()`` over a one-element
    group equals ``1 + r``.
    """
    if series is None:
        return pd.Series(dtype=float)

    normalized = pd.Series(series).copy()
    if normalized.empty:
        return pd.Series(dtype=float)

    normalized = pd.to_numeric(normalized, errors="coerce").dropna()
    if normalized.empty:
        return pd.Series(dtype=float)

    index = normalized.index
    if isinstance(index, pd.DatetimeIndex):
        timestamp_index: pd.DatetimeIndex | None = index
    elif is_numeric_dtype(index):
        timestamp_index = None
    else:
        parsed = pd.to_datetime(index, utc=True, errors="coerce")
        timestamp_index = parsed if isinstance(parsed, pd.DatetimeIndex) else None

    if isinstance(timestamp_index, pd.DatetimeIndex) and not timestamp_index.isna().all():
        normalized.index = timestamp_index
        normalized = normalized[~normalized.index.isna()]
        if normalized.empty:
            return pd.Series(dtype=float)
        normalized = ((1.0 + normalized).groupby(normalized.index.normalize()).prod() - 1.0).astype(
            float
        )

    return normalized.sort_index()


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
        [
            (_clean_returns_series(series) * weight).rename(name)
            for name, weight, series in weighted_series
        ],
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
    return [
        {**row, "weight": max(float(row.get("weight", 0.0) or 0.0), 0.0) / total} for row in rows
    ]


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


def build_series_payload(
    returns: pd.Series | None, starting_equity: float = 100_000.0
) -> _PayloadDict:
    """Build the canonical ``SeriesPayload`` dict from a returns Series.

    Delegates equity + drawdown math to the existing
    :func:`build_series_from_returns` helper (single compute path — the
    QuantStats HTML and the native charts MUST derive from the same math,
    or they'll diverge on compounding conventions) and adds month-end
    aggregation on top. Output shape matches
    :class:`msai.schemas.backtest.SeriesPayload` so the dict round-trips
    through Pydantic validation.

    Returns an empty payload ``{"daily": [], "monthly_returns": []}`` if the
    input is empty or ``None``.
    """
    daily_returns = normalize_daily_returns(returns)
    if daily_returns.empty:
        return {"daily": [], "monthly_returns": []}

    # Delegate to existing helper for equity/drawdown math — same formula,
    # same DataFrame shape [timestamp, returns, equity, drawdown].
    frame = build_series_from_returns(daily_returns, base_value=starting_equity)
    if frame.empty:
        return {"daily": [], "monthly_returns": []}

    # ``itertuples(index=False)`` avoids the per-row Series allocation that
    # ``iterrows()`` produces. Timestamps come from a DatetimeIndex via
    # ``build_series_from_returns`` so ``strftime`` is supported directly.
    daily: list[_DailyPointDict] = [
        {
            "date": row.timestamp.strftime("%Y-%m-%d"),
            "equity": float(row.equity),
            "drawdown": float(row.drawdown),
            "daily_return": float(row.returns),
        }
        for row in frame.itertuples(index=False)
    ]

    # Monthly returns: compound daily into month-end aggregates
    monthly_series = (1.0 + daily_returns).resample("ME").prod() - 1.0
    monthly_returns: list[_MonthlyReturnDict] = [
        {"month": pd.Timestamp(ts).strftime("%Y-%m"), "pct": float(pct)}
        for ts, pct in zip(monthly_series.index, monthly_series, strict=True)
    ]

    return {"daily": daily, "monthly_returns": monthly_returns}


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
    cleaned = (
        pd.to_numeric(series, errors="coerce")
        .replace([float("inf"), float("-inf")], pd.NA)
        .dropna()
    )
    if not isinstance(cleaned.index, pd.DatetimeIndex):
        cleaned.index = pd.to_datetime(cleaned.index, utc=True, errors="coerce")
    cleaned = cleaned[~cleaned.index.isna()]
    return cleaned.astype(float).sort_index()


def _safe_ratio(numerator: float, denominator: float) -> float:
    if abs(denominator) <= 1e-12:
        return 0.0
    return numerator / denominator
