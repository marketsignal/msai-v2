"""Pure computation helpers for the portfolio backtest pipeline.

These helpers are pure functions (or pure-ish — :func:`load_benchmark_returns`
reads parquet data via the same :class:`MarketDataQuery` the rest of the
system uses, but it does not mutate state and returns a pandas Series).
They have no DB session, no Nautilus dependency, no QuantStats side
effects, and are easy to unit-test in isolation.

Task A3 split these out of ``portfolio.orchestration`` so the upcoming
``portfolio_backtest/optimizer.py`` (Task E1) can reuse them without
pulling in the orchestration DAG.  Task A4 then swept every call site
onto these new names and deleted the legacy
``msai.services.portfolio_service`` shim, so the underscore-prefixed
aliases are gone.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import pandas as pd

from msai.core.logging import get_logger
from msai.models.portfolio_enums import PortfolioObjective
from msai.services.analytics_math import (
    combine_weighted_returns,
    compute_series_metrics,
)

if TYPE_CHECKING:
    from msai.services.market_data_query import MarketDataQuery

log = get_logger(__name__)


def heuristic_weight(metrics: dict[str, Any], objective: PortfolioObjective) -> float:
    """Derive a heuristic pre-normalization weight from candidate metrics.

    Returns the relevant metric when it is positive; falls back to ``1.0``
    when the metric is zero, negative, or missing so the candidate still
    participates in the portfolio.  Normalization downstream rescales the
    weight proportionally.

    Every value in :class:`PortfolioObjective` MUST be handled explicitly —
    silently falling through to equal-weight for a new enum value (B2
    expansion shipped ``MAXIMIZE_CALMAR`` / ``MINIMIZE_MAX_DRAWDOWN``) would
    quietly defeat the operator's chosen objective.  Reviewers caught this
    silent-fall-through during Phase 5.1.
    """
    if objective is PortfolioObjective.MAXIMIZE_PROFIT:
        return max(float(metrics.get("total_return") or 0.0), 0.0) or 1.0
    if objective is PortfolioObjective.MAXIMIZE_SORTINO:
        return max(float(metrics.get("sortino") or 0.0), 0.0) or 1.0
    if objective is PortfolioObjective.MAXIMIZE_SHARPE:
        return max(float(metrics.get("sharpe") or 0.0), 0.0) or 1.0
    if objective is PortfolioObjective.MAXIMIZE_CALMAR:
        # Prefer an explicit ``calmar`` metric when the candidate exposes
        # one; otherwise derive it as ``total_return / |max_drawdown|`` —
        # the same formula the optimizer's ``_score_calmar`` uses.  Floor
        # at zero and fall back to ``1.0`` when no usable signal is
        # available so the candidate still participates.
        calmar_metric = metrics.get("calmar")
        if calmar_metric is not None:
            return max(float(calmar_metric), 0.0) or 1.0
        total_return = float(metrics.get("total_return") or 0.0)
        max_dd = float(metrics.get("max_drawdown") or 0.0)
        if max_dd == 0.0:
            return 1.0
        return max(total_return / abs(max_dd), 0.0) or 1.0
    if objective is PortfolioObjective.MINIMIZE_MAX_DRAWDOWN:
        # Smaller drawdowns → higher weight (1 / |drawdown|).  ``1e-9``
        # floor prevents div-by-zero while still giving strategies with no
        # observed drawdown a finite-but-large weight (they look ideal,
        # which is what "minimize max drawdown" wants — normalisation
        # downstream rescales).
        max_dd = float(metrics.get("max_drawdown") or 0.0)
        return 1.0 / max(abs(max_dd), 1e-9)
    # EQUAL_WEIGHT / MANUAL → equal notional pre-normalization.  The
    # ``test_heuristic_weight_handles_all_objectives`` parametrised test
    # walks every PortfolioObjective enum member and asserts no future
    # value silently falls through to this branch.
    return 1.0


def effective_leverage(
    *,
    weighted_series: list[tuple[str, float, pd.Series]],
    requested_leverage: float,
    downside_target: float | None,
) -> float:
    """Scale requested leverage down so combined downside risk ≤ target.

    Computes the combined portfolio's downside risk at ``leverage=1.0``;
    if it exceeds ``downside_target``, scales leverage proportionally.
    Never scales up past the requested leverage and never below ``0.1``
    (operator safety floor).  A zero or missing ``downside_target`` (or a
    requested leverage of zero) disables scaling and returns the
    requested value verbatim.
    """
    leverage = max(0.0, float(requested_leverage))
    if leverage <= 0.0 or downside_target is None or downside_target <= 0.0:
        return leverage

    combined = combine_weighted_returns(weighted_series, leverage=1.0)
    metrics = compute_series_metrics(combined)
    downside_risk = float(metrics.downside_risk)
    if downside_risk <= 0.0 or not math.isfinite(downside_risk):
        return leverage
    scale = min(1.0, float(downside_target) / downside_risk)
    return max(0.1, leverage * scale)


def raw_benchmark_symbol(symbol: str) -> str:
    """Derive the parquet-key ticker from an operator-provided symbol.

    The MSAI ingestion pipeline stores bars under
    ``{asset_class}/{ticker}`` where ``ticker`` is the raw symbol (no
    venue suffix).  Operators sometimes type the symbol with a venue
    suffix for clarity (``SPY.NASDAQ``) and sometimes without
    (``BRK.B`` — a share-class symbol that contains a dot).

    Strip ONLY when the trailing segment looks like an uppercase venue
    code (≥2 chars, all letters).  Single-letter suffixes like ``.B`` /
    ``.A`` are share classes, never venues, so they're preserved.  This
    prevents the silent substitution of ``BRK.B`` → ``BRK`` when the
    parquet store happens to have ``BRK`` data — that would compute
    alpha/beta and the tearsheet against the wrong asset.
    """
    if "." not in symbol:
        return symbol
    head, _, tail = symbol.rpartition(".")
    if len(tail) >= 2 and tail.isalpha() and tail.isupper():
        return head
    return symbol


def load_benchmark_returns(
    market_data_query: MarketDataQuery,
    *,
    benchmark_symbol: Any,
    start_date: str,
    end_date: str,
) -> pd.Series | None:
    """Fetch a benchmark returns series, or ``None`` when unavailable.

    Returns ``None`` in one of two ways:

    * **By design** — the portfolio has no benchmark symbol configured
      (silent, no log).
    * **Data problem** — benchmark requested but unavailable or malformed
      (logged at warning level with ``symbol`` / ``start_date`` /
      ``end_date`` so the operator can diagnose).  Alpha/beta will simply
      be absent from the resulting metrics.

    The benchmark is optional by contract, so any malformed data
    (unparseable timestamps, coerce-to-NaN closes) degrades to ``None``
    rather than failing the whole portfolio run.  The intraday series is
    resampled to daily returns — for multi-year portfolios, fetching
    minute bars and computing alpha/beta against ~500k intraday points
    both wastes memory and mismatches the typical analytics frequency.
    """
    symbol = str(benchmark_symbol or "").strip()
    if not symbol:
        return None

    # Try the full symbol first (preserves share-class tickers like
    # ``BRK.B``); fall back to stripping a trailing segment for operators
    # who typed a venue suffix (``SPY.NASDAQ``).
    rows = market_data_query.get_bars(symbol, start_date, end_date, interval="1m")
    used_symbol = symbol
    if not rows:
        stripped = raw_benchmark_symbol(symbol)
        if stripped != symbol:
            rows = market_data_query.get_bars(stripped, start_date, end_date, interval="1m")
            used_symbol = stripped
    if not rows:
        log.warning(
            "benchmark_returns_no_bars",
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )
        return None
    frame = pd.DataFrame(rows)
    if "timestamp" not in frame.columns or "close" not in frame.columns:
        log.warning(
            "benchmark_returns_missing_columns",
            symbol=used_symbol,
            columns=list(frame.columns),
        )
        return None
    # ``errors="coerce"`` turns unparseable timestamps into NaT so we can
    # drop them cleanly.  The previous ``errors="raise"`` (default) would
    # abort the entire portfolio run on a single bad row even though the
    # benchmark is optional.
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    close = frame.dropna(subset=["timestamp", "close"]).set_index("timestamp")["close"]
    if close.empty:
        log.warning(
            "benchmark_returns_empty_after_clean",
            symbol=used_symbol,
            raw_rows=len(rows),
        )
        return None
    # Resample to daily close so alpha/beta are computed at the same
    # frequency as portfolio-level analytics; intraday bars would
    # otherwise load ~500k rows per year of benchmark data.
    daily_close = close.resample("1D").last().dropna()
    if daily_close.empty:
        return None
    returns = daily_close.pct_change().fillna(0.0)
    returns.name = "benchmark_returns"
    return returns


__all__ = [
    "effective_leverage",
    "heuristic_weight",
    "load_benchmark_returns",
    "raw_benchmark_symbol",
]
