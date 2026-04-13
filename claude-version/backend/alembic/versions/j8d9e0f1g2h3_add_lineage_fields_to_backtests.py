"""add lineage fields to backtests

Revision ID: j8d9e0f1g2h3
Revises: i7c8d9e0f1g2
Create Date: 2026-04-13 13:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "j8d9e0f1g2h3"
down_revision: str = "i7c8d9e0f1g2"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("backtests", sa.Column("nautilus_version", sa.String(32), nullable=True))
    op.add_column("backtests", sa.Column("python_version", sa.String(16), nullable=True))
    op.add_column("backtests", sa.Column("data_snapshot", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("backtests", "data_snapshot")
    op.drop_column("backtests", "python_version")
    op.drop_column("backtests", "nautilus_version")
