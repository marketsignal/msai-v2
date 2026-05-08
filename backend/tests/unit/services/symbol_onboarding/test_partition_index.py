from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from msai.services.symbol_onboarding.partition_index import (
    PartitionIndexService,
    PartitionRow,
    read_parquet_footer,
)


def _write_parquet(path: Path, timestamps: list[datetime]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [1.0] * len(timestamps),
            "high": [1.1] * len(timestamps),
            "low": [0.9] * len(timestamps),
            "close": [1.0] * len(timestamps),
            "volume": [100] * len(timestamps),
        }
    )
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)


def test_read_footer_returns_min_max_and_row_count(tmp_path: Path) -> None:
    path = tmp_path / "01.parquet"
    timestamps = [
        datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        datetime(2024, 1, 15, 20, 0, tzinfo=UTC),
        datetime(2024, 1, 30, 21, 0, tzinfo=UTC),
    ]
    _write_parquet(path, timestamps)

    footer = read_parquet_footer(path)

    assert footer is not None
    assert footer.min_ts == timestamps[0]
    assert footer.max_ts == timestamps[-1]
    assert footer.row_count == 3
    assert footer.file_size > 0
    assert footer.file_mtime > 0


def test_read_footer_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert read_parquet_footer(tmp_path / "nope.parquet") is None


def test_read_footer_returns_none_when_no_timestamp_column(tmp_path: Path) -> None:
    path = tmp_path / "broken.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"foo": [1, 2, 3]})
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)

    assert read_parquet_footer(path) is None


def test_read_footer_handles_naive_timestamps(tmp_path: Path) -> None:
    # Some legacy parquet files may have naive (no-tz) timestamps. We treat
    # them as UTC for indexing — coverage scan is day-resolution so the
    # tz interpretation only matters for late-evening boundaries.
    path = tmp_path / "naive.parquet"
    naive = [datetime(2024, 1, 2, 14, 30), datetime(2024, 1, 30, 21, 0)]
    _write_parquet(path, naive)

    footer = read_parquet_footer(path)

    assert footer is not None
    assert footer.min_ts.date() == naive[0].date()
    assert footer.max_ts.date() == naive[1].date()


@pytest.mark.asyncio
async def test_service_get_returns_cached_row_when_mtime_size_match(
    tmp_path: Path,
) -> None:
    # Build a real parquet file the service can stat.
    path = tmp_path / "01.parquet"
    _write_parquet(path, [datetime(2024, 1, 2, tzinfo=UTC)])
    stat = path.stat()

    cached = PartitionRow(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        min_ts=datetime(2024, 1, 2, tzinfo=UTC),
        max_ts=datetime(2024, 1, 2, tzinfo=UTC),
        row_count=1,
        file_mtime=stat.st_mtime,
        file_size=stat.st_size,
        file_path=str(path),
    )

    db = AsyncMock()
    db.fetch_one = AsyncMock(return_value=cached)
    db.upsert = AsyncMock()
    svc = PartitionIndexService(db_gateway=db)

    row = await svc.get(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        path=path,
    )

    assert row == cached
    db.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_service_get_re_reads_footer_when_mtime_changed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "01.parquet"
    _write_parquet(path, [datetime(2024, 1, 2, tzinfo=UTC)])

    stale = PartitionRow(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        min_ts=datetime(2024, 1, 2, tzinfo=UTC),
        max_ts=datetime(2024, 1, 2, tzinfo=UTC),
        row_count=1,
        file_mtime=0.0,  # stale
        file_size=0,
        file_path=str(path),
    )

    db = AsyncMock()
    db.fetch_one = AsyncMock(return_value=stale)
    db.upsert = AsyncMock()
    svc = PartitionIndexService(db_gateway=db)

    row = await svc.get(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        path=path,
    )

    assert row is not None
    assert row.file_mtime != 0.0
    db.upsert.assert_called_once()


@pytest.mark.asyncio
async def test_service_get_returns_none_when_file_missing_and_no_cache(
    tmp_path: Path,
) -> None:
    db = AsyncMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.upsert = AsyncMock()
    svc = PartitionIndexService(db_gateway=db)

    row = await svc.get(
        asset_class="stocks",
        symbol="AAPL",
        year=2024,
        month=1,
        path=tmp_path / "missing.parquet",
    )

    assert row is None
    db.upsert.assert_not_called()
