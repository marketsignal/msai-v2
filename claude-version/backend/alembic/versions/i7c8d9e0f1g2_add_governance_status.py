"""add governance_status column to strategies

Revision ID: i7c8d9e0f1g2
Revises: h6b7c8d9e0f1
Create Date: 2026-04-13 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision: str = "i7c8d9e0f1g2"
down_revision: str = "h6b7c8d9e0f1"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("governance_status", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategies", "governance_status")
