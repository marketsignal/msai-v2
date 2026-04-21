"""add backtest auto-heal lifecycle columns

Revision ID: y3s4t5u6v7w8
Revises: x2r3s4t5u6v7
Create Date: 2026-04-21 12:00:00.000000

Four additive, nullable columns on ``backtests`` to track an in-flight
auto-heal (on-demand data ingest triggered when a backtest hits
FailureCode.MISSING_DATA):

- ``phase``              — short lifecycle tag (e.g. "awaiting_data").
- ``progress_message``   — operator-facing free-text progress note.
- ``heal_started_at``    — wall-clock start of the heal attempt.
- ``heal_job_id``        — arq job id on the ``msai:ingest`` queue.

All four are cleared together when the heal reaches a terminal state.
They are additive and nullable so the migration is safe on a populated
``backtests`` table and does not require a backfill.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "y3s4t5u6v7w8"
down_revision: str = "x2r3s4t5u6v7"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add 4 additive nullable columns for auto-heal lifecycle tracking."""
    op.add_column("backtests", sa.Column("phase", sa.String(length=32), nullable=True))
    op.add_column("backtests", sa.Column("progress_message", sa.Text(), nullable=True))
    op.add_column(
        "backtests",
        sa.Column("heal_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("backtests", sa.Column("heal_job_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("backtests", "heal_job_id")
    op.drop_column("backtests", "heal_started_at")
    op.drop_column("backtests", "progress_message")
    op.drop_column("backtests", "phase")
