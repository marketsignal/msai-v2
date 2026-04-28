"""add trading_hours JSONB to instrument_definitions

Revision A of the instrument-cache → registry migration. Additive only —
adds the nullable JSONB column. ``MarketHoursService`` fail-opens
(returns "always tradeable") on NULL, so the column reads NULL for all
rows until the data-migration revision backfills from
``instrument_cache.trading_hours``.

Reversible: downgrade drops the column. Safe to run on a populated
instrument_definitions table — Postgres 16 ADD COLUMN with no default
is metadata-only and does not rewrite the table.

Revision: d1e2f3g4h5i6
Revises: c7d8e9f0a1b2
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d1e2f3g4h5i6"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instrument_definitions",
        sa.Column("trading_hours", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instrument_definitions", "trading_hours")
