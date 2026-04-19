"""order_attempt_audits table (Phase 1 task 1.2)

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-04-07 18:00:00.000000

Creates the ``order_attempt_audits`` table for Phase 1 task 1.2 (Codex
finding #7 — ``client_order_id`` is the stable correlation key the audit
hook uses to update a row through its state machine).

Every order intent — live or backtest, submitted or denied — gets a row.
The CHECK constraint enforces that exactly one of ``deployment_id`` /
``backtest_id`` is non-NULL so an audit row can never be orphaned from
its execution context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_attempt_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_order_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=True),
        sa.Column("backtest_id", sa.Uuid(), nullable=True),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("strategy_code_hash", sa.String(length=64), nullable=False),
        sa.Column("strategy_git_sha", sa.String(length=40), nullable=True),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=20, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("ts_attempted", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("is_live", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtests.id"]),
        sa.ForeignKeyConstraint(["deployment_id"], ["live_deployments.id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(deployment_id IS NOT NULL) OR (backtest_id IS NOT NULL)",
            name="ck_order_attempt_audits_deployment_or_backtest",
        ),
        sa.UniqueConstraint("client_order_id", name="uq_order_attempt_audits_client_order_id"),
    )
    op.create_index(
        op.f("ix_order_attempt_audits_client_order_id"),
        "order_attempt_audits",
        ["client_order_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_order_attempt_audits_deployment_id"),
        "order_attempt_audits",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_order_attempt_audits_backtest_id"),
        "order_attempt_audits",
        ["backtest_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_order_attempt_audits_strategy_id"),
        "order_attempt_audits",
        ["strategy_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_order_attempt_audits_instrument_id"),
        "order_attempt_audits",
        ["instrument_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_order_attempt_audits_broker_order_id"),
        "order_attempt_audits",
        ["broker_order_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_order_attempt_audits_broker_order_id"), table_name="order_attempt_audits"
    )
    op.drop_index(op.f("ix_order_attempt_audits_instrument_id"), table_name="order_attempt_audits")
    op.drop_index(op.f("ix_order_attempt_audits_strategy_id"), table_name="order_attempt_audits")
    op.drop_index(op.f("ix_order_attempt_audits_backtest_id"), table_name="order_attempt_audits")
    op.drop_index(op.f("ix_order_attempt_audits_deployment_id"), table_name="order_attempt_audits")
    op.drop_index(
        op.f("ix_order_attempt_audits_client_order_id"), table_name="order_attempt_audits"
    )
    op.drop_table("order_attempt_audits")
