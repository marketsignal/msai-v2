"""SQLAlchemy implementation of :class:`PartitionIndexGatewayProto`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from msai.models.parquet_partition_index import ParquetPartitionIndex
from msai.services.symbol_onboarding.partition_index import PartitionRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PartitionIndexGateway:
    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    async def fetch_one(
        self,
        *,
        asset_class: str,
        symbol: str,
        year: int,
        month: int,
    ) -> PartitionRow | None:
        stmt = select(ParquetPartitionIndex).where(
            ParquetPartitionIndex.asset_class == asset_class,
            ParquetPartitionIndex.symbol == symbol,
            ParquetPartitionIndex.year == year,
            ParquetPartitionIndex.month == month,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _to_dataclass(row)

    async def fetch_many(
        self,
        *,
        asset_class: str,
        symbol: str,
    ) -> list[PartitionRow]:
        stmt = select(ParquetPartitionIndex).where(
            ParquetPartitionIndex.asset_class == asset_class,
            ParquetPartitionIndex.symbol == symbol,
        )
        return [_to_dataclass(r) for r in (await self._session.execute(stmt)).scalars()]

    async def upsert(self, row: PartitionRow) -> None:
        stmt = pg_insert(ParquetPartitionIndex).values(
            asset_class=row.asset_class,
            symbol=row.symbol,
            year=row.year,
            month=row.month,
            min_ts=row.min_ts,
            max_ts=row.max_ts,
            row_count=row.row_count,
            file_mtime=row.file_mtime,
            file_size=row.file_size,
            file_path=row.file_path,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                ParquetPartitionIndex.asset_class,
                ParquetPartitionIndex.symbol,
                ParquetPartitionIndex.year,
                ParquetPartitionIndex.month,
            ],
            set_={
                "min_ts": stmt.excluded.min_ts,
                "max_ts": stmt.excluded.max_ts,
                "row_count": stmt.excluded.row_count,
                "file_mtime": stmt.excluded.file_mtime,
                "file_size": stmt.excluded.file_size,
                "file_path": stmt.excluded.file_path,
            },
        )
        await self._session.execute(stmt)
        await self._session.commit()


def _to_dataclass(row: ParquetPartitionIndex) -> PartitionRow:
    return PartitionRow(
        asset_class=row.asset_class,
        symbol=row.symbol,
        year=row.year,
        month=row.month,
        min_ts=row.min_ts,
        max_ts=row.max_ts,
        row_count=row.row_count,
        file_mtime=row.file_mtime,
        file_size=row.file_size,
        file_path=row.file_path,
    )
