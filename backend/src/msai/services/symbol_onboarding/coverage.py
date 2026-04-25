from __future__ import annotations

import asyncio
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

__all__ = ["CoverageReport", "compute_coverage"]

_TRAILING_EDGE_TOLERANCE_DAYS = 7


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
    today: date | None = None,
) -> CoverageReport:
    today = today or date.today()
    scan = await asyncio.to_thread(_scan_filesystem, data_root, asset_class, symbol, start, end)
    required_months = _months_in_range(start, end)
    present_months = scan.present_months

    if not present_months:
        return CoverageReport(
            status="none",
            covered_range=None,
            missing_ranges=[(start, end)],
        )

    missing = [m for m in required_months if m not in present_months]
    missing = _apply_trailing_edge_tolerance(missing, today=today)

    if not missing:
        return CoverageReport(
            status="full",
            covered_range=f"{start.isoformat()} → {end.isoformat()}",
            missing_ranges=[],
        )

    missing_ranges = _collapse_missing(missing, start=start, end=end)
    return CoverageReport(
        status="gapped",
        covered_range=_derive_covered_range(present_months, start=start, end=end),
        missing_ranges=missing_ranges,
    )


@dataclass(frozen=True, slots=True)
class _ScanResult:
    present_months: set[tuple[int, int]]


def _scan_filesystem(
    data_root: Path, asset_class: str, symbol: str, start: date, end: date
) -> _ScanResult:
    root = data_root / "parquet" / asset_class / symbol
    if not root.is_dir():
        return _ScanResult(present_months=set())
    present: set[tuple[int, int]] = set()
    for year_dir in root.iterdir():
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)
        for month_file in year_dir.iterdir():
            stem = month_file.stem
            if not stem.isdigit():
                continue
            month = int(stem)
            if 1 <= month <= 12 and month_file.suffix == ".parquet":
                present.add((year, month))
    return _ScanResult(present_months=present)


def _months_in_range(start: date, end: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return months


def _apply_trailing_edge_tolerance(
    missing: list[tuple[int, int]], *, today: date
) -> list[tuple[int, int]]:
    cutoff = today - timedelta(days=_TRAILING_EDGE_TOLERANCE_DAYS)
    return [(y, m) for (y, m) in missing if date(y, m, 1) <= cutoff]


def _collapse_missing(
    missing: list[tuple[int, int]], *, start: date, end: date
) -> list[tuple[date, date]]:
    if not missing:
        return []
    missing = sorted(missing)
    ranges: list[tuple[date, date]] = []
    run_start = missing[0]
    prev = run_start
    for current in missing[1:]:
        if _is_consecutive(prev, current):
            prev = current
            continue
        ranges.append(_run_to_date_range(run_start, prev, start=start, end=end))
        run_start = current
        prev = current
    ranges.append(_run_to_date_range(run_start, prev, start=start, end=end))
    return ranges


def _is_consecutive(a: tuple[int, int], b: tuple[int, int]) -> bool:
    y, m = a
    m += 1
    if m == 13:
        m = 1
        y += 1
    return (y, m) == b


def _run_to_date_range(
    run_start: tuple[int, int],
    run_end: tuple[int, int],
    *,
    start: date,
    end: date,
) -> tuple[date, date]:
    y0, m0 = run_start
    y1, m1 = run_end
    first = max(date(y0, m0, 1), start)
    last_day = monthrange(y1, m1)[1]
    last = min(date(y1, m1, last_day), end)
    return (first, last)


def _derive_covered_range(present_months: set[tuple[int, int]], *, start: date, end: date) -> str:
    present = sorted(present_months)
    if not present:
        return ""
    y0, m0 = present[0]
    y1, m1 = present[-1]
    first = max(date(y0, m0, 1), start)
    last_day = monthrange(y1, m1)[1]
    last = min(date(y1, m1, last_day), end)
    return f"{first.isoformat()} → {last.isoformat()}"
