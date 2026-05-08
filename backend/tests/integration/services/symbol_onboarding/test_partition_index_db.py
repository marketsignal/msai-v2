"""Integration tests: real Postgres + real Parquet via tmp_path.

Uses the local ``db_session`` fixture (see this directory's
``conftest.py``), which wraps the shared ``session_factory`` fixture
into a single :class:`AsyncSession` per test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from msai.services.symbol_onboarding.partition_index import (
    PartitionIndexService,
    PartitionRow,
)
from msai.services.symbol_onboarding.partition_index_db import PartitionIndexGateway

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


def _write_parquet(path: Path, timestamps: list[datetime]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({"timestamp": timestamps, "close": [1.0] * len(timestamps)})
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path)


@pytest.mark.asyncio
async def test_upsert_then_fetch_round_trip(db_session: AsyncSession, tmp_path: Path) -> None:
    path = tmp_path / "01.parquet"
    _write_parquet(path, [datetime(2024, 1, 2, tzinfo=UTC)])
    stat = path.stat()

    gw = PartitionIndexGateway(session=db_session)
    row = PartitionRow(
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
    await gw.upsert(row)

    fetched = await gw.fetch_one(asset_class="stocks", symbol="AAPL", year=2024, month=1)
    assert fetched == row


@pytest.mark.asyncio
async def test_service_full_path_with_real_db(db_session: AsyncSession, tmp_path: Path) -> None:
    path = tmp_path / "01.parquet"
    _write_parquet(
        path,
        [
            datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
            datetime(2024, 1, 30, 21, 0, tzinfo=UTC),
        ],
    )

    gw = PartitionIndexGateway(session=db_session)
    svc = PartitionIndexService(db_gateway=gw)

    row = await svc.get(asset_class="stocks", symbol="AAPL", year=2024, month=1, path=path)

    assert row is not None
    assert row.row_count == 2
    assert row.min_ts.date().day == 2
    assert row.max_ts.date().day == 30
