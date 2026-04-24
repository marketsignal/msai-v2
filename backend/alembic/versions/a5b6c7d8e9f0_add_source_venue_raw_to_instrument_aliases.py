"""add source_venue_raw to instrument_aliases

Revision ID: a5b6c7d8e9f0
Revises: z4x5y6z7a8b9
Create Date: 2026-04-23 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a5b6c7d8e9f0"
down_revision = "z4x5y6z7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instrument_aliases",
        sa.Column("source_venue_raw", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instrument_aliases", "source_venue_raw")
