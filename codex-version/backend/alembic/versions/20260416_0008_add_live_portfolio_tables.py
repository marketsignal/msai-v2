"""add live portfolio composition layer

Revision ID: 20260416_0008
Revises: 20260407_0006
Create Date: 2026-04-16 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260416_0008"
down_revision: str | None = "20260408_0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_portfolios",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "live_portfolio_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("portfolio_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("composition_hash", sa.String(length=64), nullable=False),
        sa.Column("is_frozen", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["portfolio_id"], ["live_portfolios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portfolio_id", "revision_number", name="uq_live_portfolio_revisions_number"),
        sa.UniqueConstraint("portfolio_id", "composition_hash", name="uq_live_portfolio_revisions_hash"),
    )
    op.create_index(
        "uq_one_draft_per_portfolio",
        "live_portfolio_revisions",
        ["portfolio_id"],
        unique=True,
        postgresql_where=sa.text("is_frozen = false"),
    )
    op.create_table(
        "live_portfolio_revision_strategies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_id", sa.String(length=36), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("instruments", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("weight", sa.Numeric(precision=8, scale=6), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("weight > 0 AND weight <= 1", name="ck_lprs_weight_range"),
        sa.ForeignKeyConstraint(["revision_id"], ["live_portfolio_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "order_index", name="uq_lprs_revision_order"),
        sa.UniqueConstraint("revision_id", "strategy_id", name="uq_lprs_revision_strategy"),
    )
    op.create_table(
        "live_deployment_strategies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("deployment_id", sa.String(length=36), nullable=False),
        sa.Column("revision_strategy_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_id_full", sa.String(length=280), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["live_deployments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["revision_strategy_id"],
            ["live_portfolio_revision_strategies.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "revision_strategy_id", name="uq_lds_deployment_revision_strategy"),
    )

    op.add_column("live_deployments", sa.Column("portfolio_revision_id", sa.String(length=36), nullable=True))
    op.add_column("live_deployments", sa.Column("identity_signature", sa.String(length=64), nullable=True))
    op.add_column("live_deployments", sa.Column("deployment_slug", sa.String(length=32), nullable=True))
    op.add_column("live_deployments", sa.Column("strategy_id_full", sa.String(length=280), nullable=True))
    op.add_column("live_deployments", sa.Column("account_id", sa.String(length=100), nullable=True))
    op.create_foreign_key(
        "fk_live_deployments_portfolio_revision_id",
        "live_deployments",
        "live_portfolio_revisions",
        ["portfolio_revision_id"],
        ["id"],
    )
    op.create_index(
        "ix_live_deployments_identity_signature",
        "live_deployments",
        ["identity_signature"],
        unique=True,
    )
    op.create_index(
        "ix_live_deployments_deployment_slug",
        "live_deployments",
        ["deployment_slug"],
        unique=True,
    )
    op.create_index(
        "ix_live_deployments_portfolio_revision_id",
        "live_deployments",
        ["portfolio_revision_id"],
        unique=False,
    )
    op.create_index("ix_live_deployments_account_id", "live_deployments", ["account_id"], unique=False)
    op.create_unique_constraint(
        "uq_live_deployments_revision_account",
        "live_deployments",
        ["portfolio_revision_id", "account_id"],
    )

    op.add_column("live_order_events", sa.Column("strategy_id_full", sa.String(length=280), nullable=True))
    op.create_index(
        "idx_live_order_events_strategy_full",
        "live_order_events",
        ["strategy_id_full"],
        unique=False,
    )
    op.add_column("trades", sa.Column("strategy_id_full", sa.String(length=280), nullable=True))
    op.create_index("idx_trades_strategy_full", "trades", ["strategy_id_full"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_trades_strategy_full", table_name="trades")
    op.drop_column("trades", "strategy_id_full")
    op.drop_index("idx_live_order_events_strategy_full", table_name="live_order_events")
    op.drop_column("live_order_events", "strategy_id_full")

    op.drop_constraint("uq_live_deployments_revision_account", "live_deployments", type_="unique")
    op.drop_index("ix_live_deployments_account_id", table_name="live_deployments")
    op.drop_index("ix_live_deployments_portfolio_revision_id", table_name="live_deployments")
    op.drop_index("ix_live_deployments_deployment_slug", table_name="live_deployments")
    op.drop_index("ix_live_deployments_identity_signature", table_name="live_deployments")
    op.drop_constraint("fk_live_deployments_portfolio_revision_id", "live_deployments", type_="foreignkey")
    op.drop_column("live_deployments", "account_id")
    op.drop_column("live_deployments", "strategy_id_full")
    op.drop_column("live_deployments", "deployment_slug")
    op.drop_column("live_deployments", "identity_signature")
    op.drop_column("live_deployments", "portfolio_revision_id")

    op.drop_table("live_deployment_strategies")
    op.drop_table("live_portfolio_revision_strategies")
    op.drop_index("uq_one_draft_per_portfolio", table_name="live_portfolio_revisions")
    op.drop_table("live_portfolio_revisions")
    op.drop_table("live_portfolios")
