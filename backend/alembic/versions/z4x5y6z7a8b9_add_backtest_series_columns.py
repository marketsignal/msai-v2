"""add backtest series + series_status columns

Revision ID: z4x5y6z7a8b9
Revises: y3s4t5u6v7w8
Create Date: 2026-04-21

Adds two columns to ``backtests``:

- ``series``         — JSONB NULL, canonical daily-normalized payload
                       (equity curve, drawdown series, monthly
                       aggregation, daily returns). Nullable so legacy
                       rows and failed-materialize rows carry no payload.
- ``series_status``  — VARCHAR(32) NOT NULL DEFAULT 'not_materialized'
                       with a CHECK constraint limiting the value to
                       {'ready', 'not_materialized', 'failed'}.
                       Disambiguates "ready" (payload populated) from
                       "not_materialized" (legacy / never computed) and
                       "failed" (worker hit an error while building the
                       payload).

Both additions are metadata-only on Postgres 16 — no table rewrite and
no backfill required on populated ``backtests`` tables. The CHECK is
created as a separate statement after column creation so the default
for existing rows is honored before the constraint is enforced.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "z4x5y6z7a8b9"
down_revision: str = "y3s4t5u6v7w8"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add series JSONB NULL + series_status VARCHAR(32) NOT NULL DEFAULT + CHECK."""
    op.add_column(
        "backtests",
        sa.Column("series", JSONB(), nullable=True),
    )
    op.add_column(
        "backtests",
        sa.Column(
            "series_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'not_materialized'"),
        ),
    )
    # DB-level invariant guard. The API's response model narrows this to a
    # ``Literal[...]`` and will 500 at read-time on any other value, so a
    # direct SQL write that bypassed the ORM could silently poison reads.
    # The CHECK keeps the column honest regardless of writer.
    op.create_check_constraint(
        "ck_backtests_series_status",
        "backtests",
        "series_status IN ('ready', 'not_materialized', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_backtests_series_status", "backtests", type_="check")
    op.drop_column("backtests", "series_status")
    op.drop_column("backtests", "series")
