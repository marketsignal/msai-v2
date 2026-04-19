"""add client_order_id to trades for position PnL attribution

Revision ID: m1g2h3i4j5k6
Revises: l0f1g2h3i4j5
Create Date: 2026-04-16 02:00:00.000000

Phase 2 #4 follow-up: PositionClosed events carry the
``closing_order_id`` which is the ``client_order_id`` of the fill
that closed the position. Storing ``client_order_id`` on the Trade
row lets us UPDATE ``pnl = realized_pnl`` via a single-column match
instead of a multi-table join through ``order_attempt_audits``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "m1g2h3i4j5k6"
down_revision: str = "l0f1g2h3i4j5"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("client_order_id", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_trades_client_order_id",
        "trades",
        ["client_order_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_trades_client_order_id", table_name="trades")
    op.drop_column("trades", "client_order_id")
