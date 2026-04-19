"""enforce portfolio_revision_id NOT NULL

Revision ID: u9p0q1r2s3t4
Revises: t8o9p0q1r2s3
Create Date: 2026-04-16 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "u9p0q1r2s3t4"
down_revision: str = "t8o9p0q1r2s3"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column("live_deployments", "portfolio_revision_id", nullable=False)


def downgrade() -> None:
    op.alter_column("live_deployments", "portfolio_revision_id", nullable=True)
