"""add live_portfolios, revisions, revision_strategies, deployment_strategies

Revision ID: o3i4j5k6l7m8
Revises: n2h3i4j5k6l7
Create Date: 2026-04-16 17:00:00.000000

PR #1 of the portfolio-per-account-live feature (design doc
docs/plans/2026-04-16-portfolio-per-account-live-design.md).

Adds the live-composition layer. No FK cycle — the "latest revision"
of a portfolio is computed on the fly via
``RevisionService.get_active_revision`` (order by ``revision_number``
desc + ``is_frozen=true``). This keeps the schema graph acyclic and
avoids cascade-delete-on-self semantics under existing
``drop_all/create_all`` fixtures.

Partial unique index ``uq_one_draft_per_portfolio`` enforces at
most one unfrozen revision per portfolio so concurrent
``add_strategy`` calls cannot race into two parallel drafts.

All ID columns use ``sa.Uuid()`` to match the convention from
``h6b7c8d9e0f1`` and ``n2h3i4j5k6l7``.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op

revision: str = "o3i4j5k6l7m8"
down_revision: str = "n2h3i4j5k6l7"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "live_portfolios",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_live_portfolios_name"),
    )

    op.create_table(
        "live_portfolio_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "portfolio_id",
            sa.Uuid(),
            sa.ForeignKey("live_portfolios.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("composition_hash", sa.String(64), nullable=False),
        sa.Column(
            "is_frozen",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "portfolio_id",
            "revision_number",
            name="uq_live_portfolio_revisions_number",
        ),
        sa.UniqueConstraint(
            "portfolio_id",
            "composition_hash",
            name="uq_live_portfolio_revisions_hash",
        ),
    )
    # Partial unique index: at most one unfrozen (draft) revision per
    # portfolio. Prevents two concurrent add_strategy callers from
    # racing into two parallel drafts. Uses the Alembic-native
    # ``postgresql_where`` kwarg (idiomatic for partial indexes)
    # rather than raw SQL so autogenerate diffs stay clean.
    op.create_index(
        "uq_one_draft_per_portfolio",
        "live_portfolio_revisions",
        ["portfolio_id"],
        unique=True,
        postgresql_where=sa.text("is_frozen = false"),
    )

    op.create_table(
        "live_portfolio_revision_strategies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "revision_id",
            sa.Uuid(),
            sa.ForeignKey("live_portfolio_revisions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "strategy_id",
            sa.Uuid(),
            sa.ForeignKey("strategies.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        ),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("instruments", ARRAY(sa.String()), nullable=False),
        sa.Column("weight", sa.Numeric(8, 6), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "revision_id", "order_index", name="uq_lprs_revision_order"
        ),
        sa.UniqueConstraint(
            "revision_id", "strategy_id", name="uq_lprs_revision_strategy"
        ),
    )

    op.create_table(
        "live_deployment_strategies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "deployment_id",
            sa.Uuid(),
            sa.ForeignKey("live_deployments.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "revision_strategy_id",
            sa.Uuid(),
            sa.ForeignKey(
                "live_portfolio_revision_strategies.id", ondelete="RESTRICT"
            ),
            nullable=False,
            index=True,
        ),
        sa.Column("strategy_id_full", sa.String(280), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "deployment_id",
            "revision_strategy_id",
            name="uq_lds_deployment_revision_strategy",
        ),
    )

    op.add_column(
        "live_deployments",
        sa.Column("ib_login_key", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_live_deployments_ib_login_key",
        "live_deployments",
        ["ib_login_key"],
    )

    op.add_column(
        "live_node_processes",
        sa.Column("gateway_session_key", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_live_node_processes_gateway_session_key",
        "live_node_processes",
        ["gateway_session_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_live_node_processes_gateway_session_key",
        table_name="live_node_processes",
    )
    op.drop_column("live_node_processes", "gateway_session_key")

    op.drop_index(
        "ix_live_deployments_ib_login_key",
        table_name="live_deployments",
    )
    op.drop_column("live_deployments", "ib_login_key")

    op.drop_table("live_deployment_strategies")
    op.drop_table("live_portfolio_revision_strategies")
    op.drop_index(
        "uq_one_draft_per_portfolio",
        table_name="live_portfolio_revisions",
    )
    op.drop_table("live_portfolio_revisions")
    op.drop_table("live_portfolios")
