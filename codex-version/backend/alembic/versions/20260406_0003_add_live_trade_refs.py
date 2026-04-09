"""add live trade reference columns

Revision ID: 20260406_0003
Revises: 20260406_0002
Create Date: 2026-04-06 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260406_0003"
down_revision: str | None = "20260406_0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("broker_trade_id", sa.String(length=100), nullable=True))
    op.add_column("trades", sa.Column("client_order_id", sa.String(length=100), nullable=True))
    op.add_column("trades", sa.Column("venue_order_id", sa.String(length=100), nullable=True))
    op.add_column("trades", sa.Column("position_id", sa.String(length=100), nullable=True))
    op.add_column("trades", sa.Column("broker_account_id", sa.String(length=100), nullable=True))
    op.create_index("idx_trades_broker_trade", "trades", ["broker_trade_id"], unique=False)
    op.create_unique_constraint(
        "uq_trades_deployment_broker_trade",
        "trades",
        ["deployment_id", "broker_trade_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_trades_deployment_broker_trade", "trades", type_="unique")
    op.drop_index("idx_trades_broker_trade", table_name="trades")
    op.drop_column("trades", "broker_account_id")
    op.drop_column("trades", "position_id")
    op.drop_column("trades", "venue_order_id")
    op.drop_column("trades", "client_order_id")
    op.drop_column("trades", "broker_trade_id")
