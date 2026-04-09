"""add instrument definitions cache

Revision ID: 20260406_0004
Revises: 20260406_0003
Create Date: 2026-04-06 22:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260406_0004"
down_revision: str | None = "20260406_0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_definitions",
        sa.Column("instrument_id", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("raw_symbol", sa.String(length=100), nullable=False),
        sa.Column("venue", sa.String(length=32), nullable=False),
        sa.Column("instrument_type", sa.String(length=64), nullable=False),
        sa.Column("security_type", sa.String(length=32), nullable=True),
        sa.Column("asset_class", sa.String(length=32), nullable=False),
        sa.Column("instrument_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("contract_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("instrument_id"),
    )
    op.create_index(
        "idx_instrument_definitions_raw_symbol",
        "instrument_definitions",
        ["raw_symbol"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_instrument_definitions_raw_symbol", table_name="instrument_definitions")
    op.drop_table("instrument_definitions")
