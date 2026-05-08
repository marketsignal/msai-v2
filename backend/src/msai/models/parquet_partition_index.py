"""Partition-level metadata cache for Parquet files in the bar store.

Rows are keyed by ``(asset_class, symbol, year, month)``. Cached
fields are: footer ``min_ts`` / ``max_ts`` (the actual data window
inside the partition file), ``row_count``, and the file's POSIX
``mtime`` + ``size`` for cache-invalidation. ``compute_coverage``
reads this table instead of opening every parquet footer on every
inventory request.

Cache invariants (Hawk prereq #6):
    1. ``ParquetStore.write_bars`` calls ``refresh_for_partition``
       AFTER each successful atomic write.
    2. ``PartitionIndexService.get`` re-reads the footer if either
       ``file_mtime`` or ``file_size`` no longer matches the on-disk
       file (defends against out-of-band file replacement).
    3. The one-time backfill script
       ``scripts/build_partition_index.py`` populates the table from
       a clean filesystem walk for every existing partition.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — SQLAlchemy Mapped[] resolves at runtime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base


class ParquetPartitionIndex(Base):
    __tablename__ = "parquet_partition_index"

    __table_args__ = (
        CheckConstraint("month >= 1 AND month <= 12", name="ck_partition_index_month_range"),
        CheckConstraint("row_count >= 0", name="ck_partition_index_row_count_nonneg"),
        CheckConstraint("file_size >= 0", name="ck_partition_index_file_size_nonneg"),
        CheckConstraint("max_ts >= min_ts", name="ck_partition_index_ts_order"),
    )

    asset_class: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[int] = mapped_column(Integer, primary_key=True)

    min_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_mtime: Mapped[float] = mapped_column(Float, nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)

    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
