from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from msai.services.observability import get_registry
from msai.services.observability.trading_metrics import (  # noqa: F401 — import side-effect registers the counter
    COVERAGE_GAP_DETECTED,
)
from msai.services.symbol_onboarding.coverage import compute_coverage
from msai.services.symbol_onboarding.partition_index import (
    PartitionIndexService,
    PartitionRow,
)


def _read_counter_value(metric_name: str, **labels: str) -> float:
    """Read a labeled counter value from the registry's exposition
    output. Returns 0.0 when the labeled series hasn't been touched
    yet (Prometheus convention)."""
    label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    pattern = rf"^{re.escape(metric_name)}\{{{re.escape(label_str)}\}}\s+(\S+)\s*$"
    for line in get_registry().render().splitlines():
        m = re.match(pattern, line)
        if m:
            return float(m.group(1))
    return 0.0


def _write_partition(
    base: Path,
    *,
    year: int,
    month: int,
    days: list[int],
) -> Path:
    """Write a parquet file with one bar per requested day-of-month at 16:00 UTC."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{month:02d}.parquet"
    timestamps = [datetime(year, month, d, 16, 0, tzinfo=UTC) for d in days]
    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [1.0] * len(days),
            "high": [1.1] * len(days),
            "low": [0.9] * len(days),
            "close": [1.0] * len(days),
            "volume": [100] * len(days),
        }
    )
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)
    return path


def _make_index_with_rows(rows: list[PartitionRow]) -> PartitionIndexService:
    """Build a PartitionIndexService backed by an in-memory mock gateway
    pre-populated with the given rows. The mock obeys the gateway protocol."""
    by_key = {(r.asset_class, r.symbol, r.year, r.month): r for r in rows}

    gw = AsyncMock()

    async def _fetch_one(
        *, asset_class: str, symbol: str, year: int, month: int
    ) -> PartitionRow | None:
        return by_key.get((asset_class, symbol, year, month))

    async def _fetch_many(*, asset_class: str, symbol: str) -> list[PartitionRow]:
        return [r for (ac, s, _, _), r in by_key.items() if ac == asset_class and s == symbol]

    async def _upsert(row: PartitionRow) -> None:
        by_key[(row.asset_class, row.symbol, row.year, row.month)] = row

    gw.fetch_one.side_effect = _fetch_one
    gw.fetch_many.side_effect = _fetch_many
    gw.upsert.side_effect = _upsert
    return PartitionIndexService(db_gateway=gw)


def _seed_row(
    path: Path,
    *,
    asset_class: str,
    symbol: str,
    year: int,
    month: int,
    days: list[int],
) -> PartitionRow:
    """Build a PartitionRow that mirrors what `read_parquet_footer` would
    return for the file at ``path`` written by `_write_partition` with
    the same days. Used to seed the mock cache so `compute_coverage`
    sees the same view production code would after Task 4's writer-
    side refresh has run."""
    stat = path.stat()
    timestamps = [datetime(year, month, d, 16, 0, tzinfo=UTC) for d in days]
    return PartitionRow(
        asset_class=asset_class,
        symbol=symbol,
        year=year,
        month=month,
        min_ts=min(timestamps),
        max_ts=max(timestamps),
        row_count=len(days),
        file_mtime=stat.st_mtime,
        file_size=stat.st_size,
        file_path=str(path),
    )


@pytest.mark.asyncio
async def test_intra_month_gap_is_detected(tmp_path: Path) -> None:
    """User onboards 2024-01-15 → 2024-04-30. The writer creates Jan/Feb/Mar/Apr
    parquet files but Jan only contains days 15-31. The old month-granularity
    scan would call this 'full' (all four month files exist). Day-precise
    must report 2024-01-02 through 2024-01-12 as missing trading days."""
    base = tmp_path / "parquet" / "stocks" / "AAPL"
    # Jan: only days 15-31
    jan_days = list(range(15, 32))
    feb_days = list(range(1, 30))  # 2024 is a leap year — Feb 29 is a trading day (Thu)
    mar_days = list(range(1, 32))
    apr_days = list(range(1, 31))
    p_jan = _write_partition(base / "2024", year=2024, month=1, days=jan_days)
    p_feb = _write_partition(base / "2024", year=2024, month=2, days=feb_days)
    p_mar = _write_partition(base / "2024", year=2024, month=3, days=mar_days)
    p_apr = _write_partition(base / "2024", year=2024, month=4, days=apr_days)

    index = _make_index_with_rows(
        [
            _seed_row(
                p_jan, asset_class="stocks", symbol="AAPL", year=2024, month=1, days=jan_days
            ),
            _seed_row(
                p_feb, asset_class="stocks", symbol="AAPL", year=2024, month=2, days=feb_days
            ),
            _seed_row(
                p_mar, asset_class="stocks", symbol="AAPL", year=2024, month=3, days=mar_days
            ),
            _seed_row(
                p_apr, asset_class="stocks", symbol="AAPL", year=2024, month=4, days=apr_days
            ),
        ]
    )

    report = await compute_coverage(
        asset_class="stocks",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 4, 30),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 6, 1),  # well past the window — no trailing tolerance fires
    )

    assert report.status == "gapped"
    assert len(report.missing_ranges) == 1
    miss_start, miss_end = report.missing_ranges[0]
    # 2024-01-01 is New Year's holiday; 2024-01-02 (Tue) is the first trading day.
    # Jan 12 (Fri) is the last trading day before our partition begins on Jan 15.
    assert miss_start == date(2024, 1, 2)
    assert miss_end == date(2024, 1, 12)


@pytest.mark.asyncio
async def test_trailing_edge_tolerance_forgives_recent_days(tmp_path: Path) -> None:
    """Today is 2024-01-22 (Mon). Coverage exists through Friday 2024-01-12.
    The seven trading days {Jan 16-19, 22} (skipping MLK = Jan 15) are
    inside the trailing-edge window and forgiven; status='full'."""
    base = tmp_path / "parquet" / "stocks" / "AAPL" / "2024"
    days = list(range(2, 13))
    p = _write_partition(base, year=2024, month=1, days=days)

    index = _make_index_with_rows(
        [_seed_row(p, asset_class="stocks", symbol="AAPL", year=2024, month=1, days=days)]
    )
    report = await compute_coverage(
        asset_class="stocks",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 1, 22),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 1, 22),
    )

    assert report.status == "full"
    assert report.missing_ranges == []


@pytest.mark.asyncio
async def test_older_gaps_are_NOT_forgiven(tmp_path: Path) -> None:  # noqa: N802
    """A two-week-old gap is outside the 7-day trailing-edge window and
    surfaces as 'gapped'."""
    base = tmp_path / "parquet" / "stocks" / "AAPL" / "2024"
    # Day 2 only — leaves 3-12 missing (10 trading days back from 2024-01-22).
    days = [2]
    p = _write_partition(base, year=2024, month=1, days=days)

    index = _make_index_with_rows(
        [_seed_row(p, asset_class="stocks", symbol="AAPL", year=2024, month=1, days=days)]
    )
    report = await compute_coverage(
        asset_class="stocks",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 1, 22),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 1, 22),
    )

    assert report.status == "gapped"
    assert len(report.missing_ranges) == 1


@pytest.mark.asyncio
async def test_no_data_returns_status_none(tmp_path: Path) -> None:
    index = _make_index_with_rows([])
    report = await compute_coverage(
        asset_class="stocks",
        symbol="ZZZZ",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
        partition_index=index,
        today=date(2025, 6, 1),
    )
    assert report.status == "none"
    assert report.covered_range is None
    assert report.missing_ranges == [(date(2024, 1, 1), date(2024, 12, 31))]


@pytest.mark.asyncio
async def test_full_year_coverage_returns_full(tmp_path: Path) -> None:
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    seed_rows: list[PartitionRow] = []
    for month in range(1, 13):
        # 2024 is a leap year — Feb 29 is a real trading day.
        if month == 2:
            days = list(range(1, 30))
        elif month in (4, 6, 9, 11):
            days = list(range(1, 31))
        else:
            days = list(range(1, 32))
        p = _write_partition(base, year=2024, month=month, days=days)
        seed_rows.append(
            _seed_row(p, asset_class="stocks", symbol="SPY", year=2024, month=month, days=days)
        )

    index = _make_index_with_rows(seed_rows)
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        data_root=tmp_path,
        partition_index=index,
        today=date(2025, 6, 1),
    )
    assert report.status == "full"
    assert report.missing_ranges == []
    assert report.covered_range is not None


@pytest.mark.asyncio
async def test_window_with_no_trading_days_is_full(tmp_path: Path) -> None:
    """A window like Sat→Sun (no trading days) is vacuously full.

    Semantic change from pre-Scope-B: month-granularity returned 'none' for
    any no-data window; day-precise returns 'full' when ZERO trading days
    are EXPECTED.
    """
    index = _make_index_with_rows([])
    report = await compute_coverage(
        asset_class="stocks",
        symbol="SPY",
        start=date(2024, 1, 6),
        end=date(2024, 1, 7),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 6, 1),
    )
    assert report.status == "full"
    assert report.missing_ranges == []
    assert report.covered_range is None


@pytest.mark.asyncio
async def test_gapped_emits_metric_and_alert(tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "parquet" / "stocks" / "AAPL" / "2024"
    days = [2]  # Jan 2 only — leaves 3-12 missing
    p = _write_partition(base, year=2024, month=1, days=days)

    index = _make_index_with_rows(
        [_seed_row(p, asset_class="stocks", symbol="AAPL", year=2024, month=1, days=days)]
    )

    sent_alerts: list[tuple[str, str, str]] = []

    class _StubAlerts:
        def send_alert(self, level: str, title: str, message: str) -> None:
            sent_alerts.append((level, title, message))

    monkeypatch.setattr(
        "msai.services.symbol_onboarding.coverage._get_alerting_service",
        lambda: _StubAlerts(),
    )

    before = _read_counter_value(
        "msai_coverage_gap_detected_total",
        asset_class="stocks",
        symbol="AAPL",
    )

    report = await compute_coverage(
        asset_class="stocks",
        symbol="AAPL",
        start=date(2024, 1, 1),
        end=date(2024, 1, 22),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 4, 1),
    )

    assert report.status == "gapped"
    after = _read_counter_value(
        "msai_coverage_gap_detected_total",
        asset_class="stocks",
        symbol="AAPL",
    )
    assert after >= before + 1
    assert len(sent_alerts) == 1
    level, title, message = sent_alerts[0]
    assert level in ("warning", "info")
    assert "AAPL" in title or "AAPL" in message


@pytest.mark.asyncio
async def test_status_none_does_NOT_emit_metric_or_alert(  # noqa: N802
    tmp_path: Path, monkeypatch
) -> None:
    """When the partition_index is empty for a symbol (no parquet data
    indexed), compute_coverage returns status='none' with a window-
    spanning missing range — but it MUST NOT increment the
    coverage_gap_detected metric or fire an alert. status='none' is a
    DATA-MISSING signal, not a coverage-gap signal; alert rules will be
    different.
    """
    sent_alerts: list[tuple[str, str, str]] = []

    class _StubAlerts:
        def send_alert(self, level: str, title: str, message: str) -> None:
            sent_alerts.append((level, title, message))

    monkeypatch.setattr(
        "msai.services.symbol_onboarding.coverage._get_alerting_service",
        lambda: _StubAlerts(),
    )

    before = _read_counter_value(
        "msai_coverage_gap_detected_total",
        asset_class="stocks",
        symbol="ZZZZ",
    )

    index = _make_index_with_rows([])
    report = await compute_coverage(
        asset_class="stocks",
        symbol="ZZZZ",
        start=date(2024, 1, 1),
        end=date(2024, 1, 22),
        data_root=tmp_path,
        partition_index=index,
        today=date(2024, 4, 1),
    )

    assert report.status == "none"
    after = _read_counter_value(
        "msai_coverage_gap_detected_total",
        asset_class="stocks",
        symbol="ZZZZ",
    )
    assert after == before  # NO metric increment
    assert sent_alerts == []  # NO alert
