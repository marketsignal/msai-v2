"""add backtest job lifecycle fields

Revision ID: 20260408_0007
Revises: 20260407_0006
Create Date: 2026-04-08 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260408_0007"
down_revision: str | Sequence[str] | None = "20260407_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtests", sa.Column("queue_name", sa.String(length=100), nullable=True))
    op.add_column("backtests", sa.Column("queue_job_id", sa.String(length=100), nullable=True))
    op.add_column("backtests", sa.Column("worker_id", sa.String(length=200), nullable=True))
    op.add_column("backtests", sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"))
    op.add_column("backtests", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("backtests", "attempt", server_default=None)


def downgrade() -> None:
    op.drop_column("backtests", "heartbeat_at")
    op.drop_column("backtests", "attempt")
    op.drop_column("backtests", "worker_id")
    op.drop_column("backtests", "queue_job_id")
    op.drop_column("backtests", "queue_name")
