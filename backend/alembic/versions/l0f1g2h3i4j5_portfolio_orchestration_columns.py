"""portfolio orchestration columns

Revision ID: l0f1g2h3i4j5
Revises: k9e0f1g2h3i4
Create Date: 2026-04-13 16:00:00.000000

Originally authored as revision ``k9e0f1g2h3i4`` chained off
``j8d9e0f1g2h3``. The broker_trade_id migration (PR #15) landed on
main with that same id first, so this migration was re-chained to
follow it — same DDL, new rev id.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "l0f1g2h3i4j5"
down_revision: str = "k9e0f1g2h3i4"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "portfolios",
        sa.Column("downside_target", sa.Numeric(8, 4), nullable=True),
    )
    # Relax allocation weight to nullable so the schema's "optional weight"
    # contract (heuristic derivation by objective) doesn't crash on insert.
    op.alter_column("portfolio_allocations", "weight", nullable=True)
    op.add_column(
        "portfolio_runs",
        sa.Column("max_parallelism", sa.Integer(), nullable=True),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column("series", JSONB, nullable=True),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column("allocations", JSONB, nullable=True),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "portfolio_runs",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    # Enforce portfolio_runs.status at DB level — the service layer uses a
    # StrEnum, but a DB CHECK catches raw SQL writes and stops unknown states
    # from drifting into the FSM.
    op.create_check_constraint(
        "ck_portfolio_runs_status",
        "portfolio_runs",
        "status IN ('pending', 'running', 'completed', 'failed')",
    )


def downgrade() -> None:
    # Use IF EXISTS — a previously-applied version of this migration may
    # have landed without the CHECK constraint.
    op.execute("ALTER TABLE portfolio_runs DROP CONSTRAINT IF EXISTS ck_portfolio_runs_status")
    op.drop_column("portfolio_runs", "updated_at")
    op.drop_column("portfolio_runs", "error_message")
    op.drop_column("portfolio_runs", "heartbeat_at")
    op.drop_column("portfolio_runs", "allocations")
    op.drop_column("portfolio_runs", "series")
    op.drop_column("portfolio_runs", "max_parallelism")
    # Backfill null weights with 0.0 so NOT NULL can be reapplied cleanly.
    op.execute("UPDATE portfolio_allocations SET weight = 0.0 WHERE weight IS NULL")
    op.alter_column("portfolio_allocations", "weight", nullable=False)
    op.drop_column("portfolios", "downside_target")
