"""add broker_trade_id to trades for live fill dedup

Revision ID: k9e0f1g2h3i4
Revises: j8d9e0f1g2h3
Create Date: 2026-04-15 06:00:00.000000

Phase 2 #4 — live trade persistence. Nautilus ``OrderFilled`` events
carry a unique ``trade_id`` per fill. We persist that as
``broker_trade_id`` so reconciliation-time fill replays (IB reports
historical fills when the engine restarts — nautilus.md gotcha 19)
get deduped via a partial unique index rather than producing
duplicate ``Trade`` rows.

The unique index is partial (``WHERE broker_trade_id IS NOT NULL``)
so backtest rows — which have no broker-side id — don't collide on
the empty ``NULL`` value.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "k9e0f1g2h3i4"
down_revision: str = "j8d9e0f1g2h3"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("broker_trade_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_trades_broker_trade_id_deployment",
        "trades",
        ["deployment_id", "broker_trade_id"],
        unique=True,
        postgresql_where=sa.text("broker_trade_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_trades_broker_trade_id_deployment", table_name="trades")
    op.drop_column("trades", "broker_trade_id")
