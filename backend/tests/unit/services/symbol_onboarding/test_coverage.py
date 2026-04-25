from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from msai.services.symbol_onboarding.coverage import (
    compute_coverage,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


@pytest.mark.asyncio
async def test_coverage_none_when_directory_missing(tmp_path: Path) -> None:
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
    )
    assert report.status == "none"
    assert report.covered_range is None
    assert len(report.missing_ranges) == 1


@pytest.mark.asyncio
async def test_coverage_full_when_every_month_present(tmp_path: Path) -> None:
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    for month in range(1, 13):
        _touch(base / f"{month:02d}.parquet")
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
    )
    assert report.status == "full"
    assert report.missing_ranges == []


@pytest.mark.asyncio
async def test_coverage_gapped_reports_missing_months(tmp_path: Path) -> None:
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    for month in [1, 2, 3, 7, 8, 9, 10, 11, 12]:
        _touch(base / f"{month:02d}.parquet")
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
    )
    assert report.status == "gapped"
    assert len(report.missing_ranges) == 1
    missing_start, missing_end = report.missing_ranges[0]
    assert missing_start == date(2024, 4, 1)
    assert missing_end == date(2024, 6, 30)


@pytest.mark.asyncio
async def test_coverage_trailing_edge_tolerance_filters_recent_months(
    tmp_path: Path,
) -> None:
    today = date(2026, 4, 24)
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2026"
    for month in [1, 2, 3]:
        _touch(base / f"{month:02d}.parquet")
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2026, 1, 1),
        end=date(2026, 5, 31),
        data_root=tmp_path,
        today=today,
    )
    assert report.status == "gapped"
    assert len(report.missing_ranges) == 1
    missing_start, missing_end = report.missing_ranges[0]
    assert missing_start == date(2026, 4, 1)
    assert missing_end == date(2026, 4, 30)
