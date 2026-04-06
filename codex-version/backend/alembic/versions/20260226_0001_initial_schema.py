"""initial schema

Revision ID: 20260226_0001
Revises:
Create Date: 2026-02-26 20:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260226_0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("entra_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=50), server_default="viewer", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("entra_id"),
    )

    op.create_table(
        "strategies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("strategy_class", sa.String(length=255), nullable=False),
        sa.Column("config_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("default_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "backtests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("strategy_id", sa.String(length=36), nullable=True),
        sa.Column("strategy_code_hash", sa.String(length=64), nullable=False),
        sa.Column("strategy_git_sha", sa.String(length=40), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("instruments", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=50), server_default="pending", nullable=False),
        sa.Column("progress", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("report_path", sa.String(length=500), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "live_deployments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("strategy_id", sa.String(length=36), nullable=True),
        sa.Column("strategy_code_hash", sa.String(length=64), nullable=False),
        sa.Column("strategy_git_sha", sa.String(length=40), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("instruments", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("status", sa.String(length=50), server_default="stopped", nullable=False),
        sa.Column("paper_trading", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["started_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "trades",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("backtest_id", sa.String(length=36), nullable=True),
        sa.Column("deployment_id", sa.String(length=36), nullable=True),
        sa.Column("strategy_id", sa.String(length=36), nullable=False),
        sa.Column("strategy_code_hash", sa.String(length=64), nullable=False),
        sa.Column("instrument", sa.String(length=100), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("commission", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("pnl", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("is_live", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(backtest_id IS NOT NULL AND deployment_id IS NULL) OR (backtest_id IS NULL AND deployment_id IS NOT NULL)",
            name="chk_trades_source",
        ),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtests.id"]),
        sa.ForeignKeyConstraint(["deployment_id"], ["live_deployments.id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_trades_backtest", "trades", ["backtest_id"])
    op.create_index("idx_trades_deployment", "trades", ["deployment_id"])
    op.create_index("idx_trades_strategy", "trades", ["strategy_id"])
    op.create_index("idx_trades_executed", "trades", ["executed_at"])
    op.create_index("idx_trades_instrument", "trades", ["instrument"])

    op.create_table(
        "strategy_daily_pnl",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.String(length=36), nullable=False),
        sa.Column("deployment_id", sa.String(length=36), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("pnl", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("cumulative_pnl", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("capital_used", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("num_trades", sa.Integer(), server_default="0", nullable=False),
        sa.Column("win_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("loss_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_drawdown", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["live_deployments.id"]),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_id", "deployment_id", "date", name="uq_strategy_daily_pnl"),
    )
    op.create_index("idx_daily_pnl_strategy", "strategy_daily_pnl", ["strategy_id", "date"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=True),
        sa.Column("resource_id", sa.String(length=36), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_audit_created", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_audit_created", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("idx_daily_pnl_strategy", table_name="strategy_daily_pnl")
    op.drop_table("strategy_daily_pnl")

    op.drop_index("idx_trades_instrument", table_name="trades")
    op.drop_index("idx_trades_executed", table_name="trades")
    op.drop_index("idx_trades_strategy", table_name="trades")
    op.drop_index("idx_trades_deployment", table_name="trades")
    op.drop_index("idx_trades_backtest", table_name="trades")
    op.drop_table("trades")

    op.drop_table("live_deployments")
    op.drop_table("backtests")
    op.drop_table("strategies")
    op.drop_table("users")
