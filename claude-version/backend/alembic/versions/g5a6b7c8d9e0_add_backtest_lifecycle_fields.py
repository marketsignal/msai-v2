"""Add backtest lifecycle fields for job watchdog.

Revision ID: g5a6b7c8d9e0
Revises: f4a5b6c7d8e9
Create Date: 2026-04-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "g5a6b7c8d9e0"
down_revision: str = "f4a5b6c7d8e9"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("backtests", sa.Column("queue_name", sa.String(100), nullable=True))
    op.add_column("backtests", sa.Column("queue_job_id", sa.String(100), nullable=True))
    op.add_column("backtests", sa.Column("worker_id", sa.String(200), nullable=True))
    op.add_column(
        "backtests",
        sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "backtests",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backtests", "heartbeat_at")
    op.drop_column("backtests", "attempt")
    op.drop_column("backtests", "worker_id")
    op.drop_column("backtests", "queue_job_id")
    op.drop_column("backtests", "queue_name")
