"""Day-precise coverage scan for parquet partitions.

For a given ``(asset_class, symbol, [start, end])`` window, build the
set of trading days the asset class's exchange calendar expects, then
subtract the set of days actually present in the cached parquet
partition footers (read from ``parquet_partition_index`` via
:class:`PartitionIndexService`). Remaining trading days are
"missing"; contiguous runs collapse into ``missing_ranges``.

The public shape of :class:`CoverageReport` is preserved so call sites
in ``api/symbol_onboarding.py`` and the onboarding orchestrator
compile unchanged. The semantics shift from "month is missing" to
"day is missing". Every call site already handled the existing
``missing_ranges: list[tuple[date, date]]``; the only difference is
that those tuples can now have intra-month spans.

Trailing-edge tolerance is now day-aligned: the most recent
``_TRAILING_EDGE_TOLERANCE_TRADING_DAYS`` trading days are forgiven so
a healthy ingest pipeline running ~T+1 doesn't trigger a stale-only
gap on every refresh. The constant is tuned to 7 trading days
(roughly two business weeks worth of slack) — long enough to cover
weekend + holiday + provider-scheduling latency, short enough that a
genuine multi-week regression still surfaces as ``stale``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from msai.services.trading_calendar import trading_days

if TYPE_CHECKING:
    from msai.services.symbol_onboarding.partition_index import (
        PartitionIndexService,
        PartitionRow,
    )

log = structlog.get_logger(__name__)

__all__ = ["CoverageReport", "compute_coverage"]

_TRAILING_EDGE_TOLERANCE_TRADING_DAYS = 7


@dataclass(frozen=True, slots=True)
class CoverageReport:
    status: Literal["full", "gapped", "none"]
    covered_range: str | None
    missing_ranges: list[tuple[date, date]]


async def compute_coverage(
    *,
    asset_class: str,
    symbol: str,
    start: date,
    end: date,
    data_root: Path,
    partition_index: PartitionIndexService,
    today: date | None = None,
) -> CoverageReport:
    today = today or date.today()
    expected_days = trading_days(start, end, asset_class=asset_class)
    if not expected_days:
        # Window entirely outside the calendar (e.g. start > end, or
        # crypto with no trading days under our convention) — vacuously
        # full. Note: Sat→Sun returns "full" here, NOT "none" — semantics
        # change from pre-Scope-B (no months → "none"). The intent is "no
        # trading days were expected, so nothing is missing."
        return CoverageReport(status="full", covered_range=None, missing_ranges=[])

    rows = await partition_index.get_for_symbol(asset_class=asset_class, symbol=symbol)
    covered_days = _covered_days_from_rows(
        rows,
        start=start,
        end=end,
        asset_class=asset_class,
    )

    if not covered_days:
        # Nothing in the partition index for this symbol that overlaps
        # [start, end]. Backfill (Task 5) populates the index from
        # existing files; the writer (Task 4) refreshes on every
        # successful write. If the index is empty here, no parquet data
        # is available — surface that as ``status="none"`` and a single
        # window-spanning missing range so the auto-heal flow sees a
        # cleanly-shaped repair request.
        return CoverageReport(
            status="none",
            covered_range=None,
            missing_ranges=[(start, end)],
        )

    missing = sorted(expected_days - covered_days)
    if missing:
        missing = _apply_trailing_edge_tolerance(
            missing,
            today=today,
            asset_class=asset_class,
        )

    if not missing:
        return CoverageReport(
            status="full",
            covered_range=_derive_covered_range(covered_days),
            missing_ranges=[],
        )

    return CoverageReport(
        status="gapped",
        covered_range=_derive_covered_range(covered_days),
        missing_ranges=_collapse_missing(missing),
    )


def _covered_days_from_rows(
    rows: list[PartitionRow],
    *,
    start: date,
    end: date,
    asset_class: str,
) -> set[date]:
    """Covered days = trading-day intersection of every partition's
    ``[min_ts.date(), max_ts.date()]`` window.

    P1-1 fix from plan-review iteration 1: the previous implementation
    walked calendar days and admitted weekends + holidays as "covered"
    whenever a partition spanned them, which silently cancelled gap
    detection for any partition with data on both the first and last
    trading day of the month. Trading-day intersection is the only
    correct definition of "this partition covers day D".

    A partition with internal gaps (provider returned days 1-5 + 15-31
    in the same January file) is the residual blind spot — see the
    "Residual: internal-partition gaps" note in Implementation Notes.

    Returns the intersection with the requested ``[start, end]`` window
    so callers see only the days they asked about.
    """
    covered: set[date] = set()
    for row in rows:
        partition_first = row.min_ts.date()
        partition_last = row.max_ts.date()
        # Clip to the requested window before asking the calendar.
        clipped_first = max(partition_first, start)
        clipped_last = min(partition_last, end)
        if clipped_first > clipped_last:
            continue
        # ``trading_days`` is vectorized via exchange_calendars'
        # ``sessions_in_range``; far cheaper than a per-day Python loop
        # even for multi-year partitions.
        covered |= trading_days(clipped_first, clipped_last, asset_class=asset_class)
    return covered


def _apply_trailing_edge_tolerance(
    missing: list[date],
    *,
    today: date,
    asset_class: str,
) -> list[date]:
    """Drop the most recent ``_TRAILING_EDGE_TOLERANCE_TRADING_DAYS``
    trading days from ``missing``.

    We compute the set of "tolerated" trading days as the last N
    trading days strictly before ``today`` (today itself is also
    tolerated since the day's bars don't usually land until after
    close). For a typical Mon-Fri market this is ``today`` plus the
    seven prior trading days.
    """
    # Look back ~3 calendar weeks to harvest 7 trading days reliably,
    # even across two long-weekend holidays.
    lookback_start = today - timedelta(days=21)
    recent = sorted(trading_days(lookback_start, today, asset_class=asset_class))
    tolerated = set(recent[-_TRAILING_EDGE_TOLERANCE_TRADING_DAYS:])
    tolerated.add(today)
    return [d for d in missing if d not in tolerated]


def _collapse_missing(missing: list[date]) -> list[tuple[date, date]]:
    """Collapse a sorted list of dates into contiguous ranges. Two
    dates are contiguous when the second is the next *trading* day
    after the first — but for the public ``missing_ranges`` shape the
    range endpoints are calendar dates, and consumers (Repair UI,
    backtest auto-heal) submit a calendar [start, end] window to
    re-fetch. So contiguity here is calendar-day adjacency on the
    sorted-trading-days list. Practically: if two trading days are
    less than 5 calendar days apart with no other trading days in
    between, treat as one run."""
    if not missing:
        return []
    ranges: list[tuple[date, date]] = []
    run_start = missing[0]
    prev = run_start
    for current in missing[1:]:
        if (current - prev).days <= 5:
            prev = current
            continue
        ranges.append((run_start, prev))
        run_start = current
        prev = current
    ranges.append((run_start, prev))
    return ranges


def _derive_covered_range(covered: set[date]) -> str:
    """Render covered-days set as ``"YYYY-MM-DD → YYYY-MM-DD"`` using
    the min and max — even if there are internal gaps. The covered_range
    field is a human-readable hint, not a contract."""
    if not covered:
        return ""
    first = min(covered)
    last = max(covered)
    return f"{first.isoformat()} → {last.isoformat()}"
