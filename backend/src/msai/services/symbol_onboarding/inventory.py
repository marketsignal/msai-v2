"""Inventory readiness aggregation: status derivation + trailing-only detection.

The page-level `/api/v1/symbols/inventory` endpoint composes the existing
`SecurityMaster.find_active_aliases` + `compute_coverage` results into a
single typed status per row. Status priority (worst-actionable wins):

    not_registered → live_only → backtest_only → gapped → stale → ready

Where the gapped/stale distinction depends on WHICH months are missing
relative to today: trailing-edge-only missing months collapse to stale
(action: refresh); any mid-window gap is gapped (action: repair).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

__all__ = ["derive_status", "is_trailing_only", "Status"]

Status = Literal[
    "ready",
    "stale",
    "gapped",
    "backtest_only",
    "live_only",
    "not_registered",
]


def is_trailing_only(
    *,
    missing_ranges: list[tuple[date, date]],
    today: date,
    asset_class: str = "equity",
) -> bool:
    """True iff there is exactly ONE missing range AND its start sits
    within the last 7 trading days for ``asset_class``'s calendar.

    The 7-day window matches the trailing-edge tolerance baked into
    ``compute_coverage``; together they form the stale ↔ gapped
    boundary the inventory page renders. ``asset_class`` defaults to
    equity for legacy callers that pre-date the day-precise refactor.
    """
    if len(missing_ranges) != 1:
        return False
    # Lazy imports to avoid pulling exchange_calendars at module-import
    # time (circular-import / cold-start cost on workers that never call
    # this function).
    from datetime import timedelta

    from msai.services.trading_calendar import trading_days

    range_start, _end = missing_ranges[0]
    lookback_start = today - timedelta(days=21)
    recent = sorted(trading_days(lookback_start, today, asset_class=asset_class))
    cutoff_idx = max(0, len(recent) - 7)
    cutoff_day = recent[cutoff_idx] if recent else today
    return range_start >= cutoff_day


def derive_status(
    *,
    registered: bool,
    bt_avail: bool,
    live: bool,
    coverage_status: Literal["full", "gapped", "none"] | None,
    missing_ranges: list[tuple[date, date]],
    today: date,
    asset_class: str = "equity",
) -> Status:
    """Resolve a single Status from the readiness signals.

    Priority (worst-actionable wins):
      not_registered → gapped (mid-window) → stale (trailing only)
      → live_only (no historical data) → backtest_only (no IB qual)
      → ready (everything green).

    Note: a `registered=True` row never returns "not_registered"; falls
    through to "live_only" (with IB) or "backtest_only" (no data / no IB
    — generalized "registered, awaiting data" state).

    ``asset_class`` is forwarded to :func:`is_trailing_only` so the
    stale ↔ gapped boundary uses the right exchange calendar; defaults
    to equity for legacy callers.
    """
    if not registered:
        return "not_registered"
    if coverage_status == "gapped":
        trailing = is_trailing_only(
            missing_ranges=missing_ranges,
            today=today,
            asset_class=asset_class,
        )
        return "stale" if trailing else "gapped"
    if coverage_status == "full" and bt_avail and live:
        return "ready"
    if not bt_avail and live:
        return "live_only"
    return "backtest_only"
