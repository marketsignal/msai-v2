"""instrument_cache table (Phase 2 task 2.2)

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-04-07 20:00:00.000000

Creates the ``instrument_cache`` table. One row per fully-resolved
Nautilus instrument, keyed by the canonical Nautilus ``InstrumentId``
string in IB simplified symbology. See the ``InstrumentCache`` model
module docstring for the ``trading_hours`` JSONB schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "f4a5b6c7d8e9"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_cache",
        sa.Column("canonical_id", sa.String(length=128), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("ib_contract_json", postgresql.JSONB(), nullable=False),
        sa.Column("nautilus_instrument_json", postgresql.JSONB(), nullable=False),
        sa.Column("trading_hours", postgresql.JSONB(), nullable=True),
        sa.Column(
            "last_refreshed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("canonical_id"),
    )
    op.create_index(
        "ix_instrument_cache_asset_class",
        "instrument_cache",
        ["asset_class"],
        unique=False,
    )
    op.create_index(
        "ix_instrument_cache_venue",
        "instrument_cache",
        ["venue"],
        unique=False,
    )
    op.create_index(
        "ix_instrument_cache_class_venue",
        "instrument_cache",
        ["asset_class", "venue"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_instrument_cache_class_venue", table_name="instrument_cache")
    op.drop_index("ix_instrument_cache_venue", table_name="instrument_cache")
    op.drop_index("ix_instrument_cache_asset_class", table_name="instrument_cache")
    op.drop_table("instrument_cache")
