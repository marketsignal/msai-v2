"""Add parquet_partition_index — footer-metadata cache for day-precise coverage.

Revision ID: aa00b11c22d3
Revises: 1e2d728f1b32
Create Date: 2026-05-07 00:00:00.000000

Cache table for Parquet partition footer metadata (min_ts, max_ts,
row_count, file_mtime, file_size). Read by ``compute_coverage`` so
the day-precise scan does not open every parquet file on every
inventory request. Refreshed by ``ParquetStore.write_bars`` and the
one-time ``scripts/build_partition_index.py`` backfill.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "aa00b11c22d3"
down_revision: str = "1e2d728f1b32"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "parquet_partition_index",
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("min_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("file_mtime", sa.Float(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint(
            "asset_class",
            "symbol",
            "year",
            "month",
            name="pk_parquet_partition_index",
        ),
        sa.CheckConstraint(
            "month >= 1 AND month <= 12",
            name="ck_partition_index_month_range",
        ),
        sa.CheckConstraint(
            "row_count >= 0",
            name="ck_partition_index_row_count_nonneg",
        ),
        sa.CheckConstraint(
            "file_size >= 0",
            name="ck_partition_index_file_size_nonneg",
        ),
        sa.CheckConstraint(
            "max_ts >= min_ts",
            name="ck_partition_index_ts_order",
        ),
    )
    op.create_index(
        "ix_partition_index_symbol",
        "parquet_partition_index",
        ["symbol", "asset_class"],
    )


def downgrade() -> None:
    op.drop_index("ix_partition_index_symbol", table_name="parquet_partition_index")
    op.drop_table("parquet_partition_index")
