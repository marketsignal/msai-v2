"""add live order event audit table

Revision ID: 20260407_0005
Revises: 20260406_0004
Create Date: 2026-04-07 10:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260407_0005"
down_revision: str | None = "20260406_0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_order_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("deployment_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_code_hash", sa.String(length=64), nullable=False),
        sa.Column("paper_trading", sa.Boolean(), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("instrument", sa.String(length=100), nullable=True),
        sa.Column("client_order_id", sa.String(length=100), nullable=True),
        sa.Column("venue_order_id", sa.String(length=100), nullable=True),
        sa.Column("broker_account_id", sa.String(length=100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ts_event", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["live_deployments.id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "event_id", name="uq_live_order_events_deployment_event"),
    )
    op.create_index(
        "idx_live_order_events_deployment",
        "live_order_events",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "idx_live_order_events_strategy",
        "live_order_events",
        ["strategy_id"],
        unique=False,
    )
    op.create_index(
        "idx_live_order_events_ts_event",
        "live_order_events",
        ["ts_event"],
        unique=False,
    )
    op.create_index(
        "idx_live_order_events_client_order",
        "live_order_events",
        ["client_order_id"],
        unique=False,
    )
    op.create_index(
        "idx_live_order_events_venue_order",
        "live_order_events",
        ["venue_order_id"],
        unique=False,
    )
    op.create_index(
        "idx_live_order_events_type",
        "live_order_events",
        ["event_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_live_order_events_type", table_name="live_order_events")
    op.drop_index("idx_live_order_events_venue_order", table_name="live_order_events")
    op.drop_index("idx_live_order_events_client_order", table_name="live_order_events")
    op.drop_index("idx_live_order_events_ts_event", table_name="live_order_events")
    op.drop_index("idx_live_order_events_strategy", table_name="live_order_events")
    op.drop_index("idx_live_order_events_deployment", table_name="live_order_events")
    op.drop_table("live_order_events")
