"""enforce NOT NULL on ib_login_key + gateway_session_key

Revision ID: t8o9p0q1r2s3
Revises: s7n8o9p0q1r2
Create Date: 2026-04-16 23:50:00.000000

PR#3 portfolio-per-account-live: enforce NOT NULL on the two session-key
columns that were added nullable in PR#1 and populated by PR#2.

Changes:
1. Backfill NULL ``ib_login_key`` on ``live_deployments`` with 'default'.
2. Alter ``ib_login_key`` to NOT NULL.
3. Backfill NULL ``gateway_session_key`` on ``live_node_processes`` with 'default'.
4. Alter ``gateway_session_key`` to NOT NULL.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "t8o9p0q1r2s3"
down_revision: str = "s7n8o9p0q1r2"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. Backfill NULL ib_login_key on live_deployments
    op.execute(
        sa.text("UPDATE live_deployments SET ib_login_key = 'default' WHERE ib_login_key IS NULL")
    )
    op.alter_column("live_deployments", "ib_login_key", nullable=False)

    # 2. Backfill NULL gateway_session_key on live_node_processes
    op.execute(
        sa.text(
            "UPDATE live_node_processes SET gateway_session_key = 'default' "
            "WHERE gateway_session_key IS NULL"
        )
    )
    op.alter_column("live_node_processes", "gateway_session_key", nullable=False)


def downgrade() -> None:
    op.alter_column("live_node_processes", "gateway_session_key", nullable=True)
    op.alter_column("live_deployments", "ib_login_key", nullable=True)
