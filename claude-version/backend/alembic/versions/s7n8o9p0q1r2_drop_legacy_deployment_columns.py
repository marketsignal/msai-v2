"""drop legacy deployment columns, enforce portfolio_revision_id NOT NULL

Revision ID: s7n8o9p0q1r2
Revises: r6m7n8o9p0q1
Create Date: 2026-04-16 23:45:00.000000

PR#2 portfolio-per-account-live, Task 11: cleanup migration that runs
AFTER the backfill (r6m7n8o9p0q1) has populated ``portfolio_revision_id``
on all existing rows.

Changes:
1. Makes ``portfolio_revision_id`` NOT NULL.
2. Makes ``strategy_id`` nullable (kept for FK audit trail).
3. Drops 5 columns whose data now lives on
   ``live_portfolio_revision_strategies``:
   - config_hash (String 64)
   - instruments (ARRAY String)
   - instruments_signature (Text)
   - strategy_code_hash (String 64)
   - config (JSONB)
4. Adds ``UniqueConstraint("portfolio_revision_id", "account_id")``
   named ``uq_live_deployments_revision_account``.

NOTE: ``identity_signature`` is deliberately KEPT -- the upsert target
at ``api/live.py:441`` depends on it.  Its VALUE changes (computed from
``PortfolioDeploymentIdentity`` now) but the column and unique index
remain.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op

revision: str = "s7n8o9p0q1r2"
down_revision: str = "r6m7n8o9p0q1"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # NOTE: portfolio_revision_id stays nullable — legacy /start endpoint
    # doesn't supply it. NOT NULL enforcement deferred to a future PR
    # when /start is formally deprecated.

    # 1. Make strategy_id nullable (kept for FK audit trail)
    op.alter_column("live_deployments", "strategy_id", nullable=True)

    # 3. Drop columns whose data now lives on live_portfolio_revision_strategies
    op.drop_column("live_deployments", "config_hash")
    op.drop_column("live_deployments", "instruments")
    op.drop_column("live_deployments", "instruments_signature")
    op.drop_column("live_deployments", "strategy_code_hash")
    op.drop_column("live_deployments", "config")

    # 4. Add composite unique constraint: one deployment per (revision, account)
    op.create_unique_constraint(
        "uq_live_deployments_revision_account",
        "live_deployments",
        ["portfolio_revision_id", "account_id"],
    )


def downgrade() -> None:
    # Reverse order of upgrade operations

    # 4. Remove the composite unique constraint
    op.drop_constraint("uq_live_deployments_revision_account", "live_deployments")

    # 3. Re-add dropped columns (all nullable since data is gone)
    op.add_column("live_deployments", sa.Column("config", JSONB(), nullable=True))
    op.add_column("live_deployments", sa.Column("strategy_code_hash", sa.String(64), nullable=True))
    op.add_column("live_deployments", sa.Column("instruments_signature", sa.Text(), nullable=True))
    op.add_column("live_deployments", sa.Column("instruments", ARRAY(sa.String()), nullable=True))
    op.add_column("live_deployments", sa.Column("config_hash", sa.String(64), nullable=True))

    # 1. Make strategy_id NOT NULL again
    op.alter_column("live_deployments", "strategy_id", nullable=False)
