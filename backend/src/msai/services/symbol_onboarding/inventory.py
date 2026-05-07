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
) -> bool:
    """True iff there is exactly ONE missing range AND it sits at the
    trailing edge (i.e., starts at today's previous-month boundary or
    later). Multi-range or older-than-prev-month-start gaps return False
    (they're "gapped", not "stale").
    """
    if len(missing_ranges) != 1:
        return False
    start, _end = missing_ranges[0]
    if today.month == 1:
        prev_month_start = date(today.year - 1, 12, 1)
    else:
        prev_month_start = date(today.year, today.month - 1, 1)
    return start >= prev_month_start


def derive_status(
    *,
    registered: bool,
    bt_avail: bool,
    live: bool,
    coverage_status: Literal["full", "gapped", "none"] | None,
    missing_ranges: list[tuple[date, date]],
    today: date,
) -> Status:
    """Resolve a single Status from the readiness signals.

    Priority (worst-actionable wins):
      not_registered → gapped (mid-window) → stale (trailing only)
      → live_only (no historical data) → backtest_only (no IB qual)
      → ready (everything green).

    Note: a `registered=True` row never returns "not_registered"; falls
    through to "live_only" (with IB) or "backtest_only" (no data / no IB
    — generalized "registered, awaiting data" state).
    """
    if not registered:
        return "not_registered"
    if coverage_status == "gapped":
        trailing = is_trailing_only(missing_ranges=missing_ranges, today=today)
        return "stale" if trailing else "gapped"
    if coverage_status == "full" and bt_avail and live:
        return "ready"
    if not bt_avail and live:
        return "live_only"
    return "backtest_only"
