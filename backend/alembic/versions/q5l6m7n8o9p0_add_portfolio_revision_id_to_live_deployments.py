"""add portfolio_revision_id FK to live_deployments

Revision ID: q5l6m7n8o9p0
Revises: p4k5l6m7n8o9
Create Date: 2026-04-16 23:00:00.000000

PR#2 portfolio-per-account-live: adds the ``portfolio_revision_id``
column to ``live_deployments`` so each deployment can reference the
frozen portfolio revision that triggered it. The column is nullable
for backward compatibility — existing rows pre-date portfolio-based
deployments. Task 10 will backfill, Task 11 will make it NOT NULL.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "q5l6m7n8o9p0"
down_revision: str = "p4k5l6m7n8o9"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "live_deployments",
        sa.Column(
            "portfolio_revision_id",
            sa.Uuid(),
            sa.ForeignKey("live_portfolio_revisions.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_live_deployments_portfolio_revision_id",
        "live_deployments",
        ["portfolio_revision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_live_deployments_portfolio_revision_id",
        table_name="live_deployments",
    )
    op.drop_column("live_deployments", "portfolio_revision_id")
