"""Add research, graduation, portfolio, and asset universe tables.

Revision ID: h6b7c8d9e0f1
Revises: g5a6b7c8d9e0
Create Date: 2026-04-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "h6b7c8d9e0f1"
down_revision: str = "g5a6b7c8d9e0"
branch_labels: tuple[str, ...] | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. asset_universe
    op.create_table(
        "asset_universe",
        sa.Column("id", sa.Uuid(), nullable=False, default=sa.text("gen_random_uuid()")),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.UniqueConstraint("symbol", "exchange", "resolution", name="uq_asset_symbol_exchange_res"),
    )
    op.create_index("ix_asset_universe_created_by", "asset_universe", ["created_by"])

    # 2. research_jobs
    op.create_table(
        "research_jobs",
        sa.Column("id", sa.Uuid(), nullable=False, default=sa.text("gen_random_uuid()")),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("progress", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("progress_message", sa.String(256), nullable=True),
        sa.Column("results", postgresql.JSONB(), nullable=True),
        sa.Column("best_config", postgresql.JSONB(), nullable=True),
        sa.Column("best_metrics", postgresql.JSONB(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_research_jobs_strategy_id", "research_jobs", ["strategy_id"])
    op.create_index("ix_research_jobs_created_by", "research_jobs", ["created_by"])

    # 3. research_trials
    op.create_table(
        "research_trials",
        sa.Column("id", sa.Uuid(), nullable=False, default=sa.text("gen_random_uuid()")),
        sa.Column("research_job_id", sa.Uuid(), nullable=False),
        sa.Column("trial_number", sa.Integer(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("objective_value", sa.Numeric(18, 8), nullable=True),
        sa.Column("backtest_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["research_job_id"], ["research_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["backtest_id"], ["backtests.id"]),
        sa.UniqueConstraint("research_job_id", "trial_number", name="uq_trial_job_number"),
    )
    op.create_index("ix_research_trials_research_job_id", "research_trials", ["research_job_id"])
    op.create_index("ix_research_trials_backtest_id", "research_trials", ["backtest_id"])

    # 4. graduation_candidates
    op.create_table(
        "graduation_candidates",
        sa.Column("id", sa.Uuid(), nullable=False, default=sa.text("gen_random_uuid()")),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("research_job_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.String(32), nullable=False, server_default="discovery"),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("promoted_by", sa.Uuid(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["strategy_id"], ["strategies.id"]),
        sa.ForeignKeyConstraint(["research_job_id"], ["research_jobs.id"]),
        sa.ForeignKeyConstraint(["deployment_id"], ["live_deployments.id"]),
        sa.ForeignKeyConstraint(["promoted_by"], ["users.id"]),
    )
    op.create_index("ix_graduation_candidates_strategy_id", "graduation_candidates", ["strategy_id"])
    op.create_index("ix_graduation_candidates_research_job_id", "graduation_candidates", ["research_job_id"])
    op.create_index("ix_graduation_candidates_deployment_id", "graduation_candidates", ["deployment_id"])
    op.create_index("ix_graduation_candidates_promoted_by", "graduation_candidates", ["promoted_by"])

    # 5. graduation_stage_transitions
    op.create_table(
        "graduation_stage_transitions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("from_stage", sa.String(32), nullable=False),
        sa.Column("to_stage", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("transitioned_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["graduation_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["transitioned_by"], ["users.id"]),
    )
    op.create_index(
        "ix_graduation_stage_transitions_candidate_id",
        "graduation_stage_transitions",
        ["candidate_id"],
    )
    op.create_index(
        "ix_graduation_stage_transitions_transitioned_by",
        "graduation_stage_transitions",
        ["transitioned_by"],
    )

    # 6. portfolios
    op.create_table(
        "portfolios",
        sa.Column("id", sa.Uuid(), nullable=False, default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("objective", sa.String(64), nullable=False),
        sa.Column("base_capital", sa.Numeric(18, 2), nullable=False),
        sa.Column(
            "requested_leverage", sa.Numeric(8, 4), nullable=False, server_default="1.0"
        ),
        sa.Column("benchmark_symbol", sa.String(32), nullable=True),
        sa.Column("account_id", sa.String(64), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_portfolios_created_by", "portfolios", ["created_by"])

    # 7. portfolio_allocations
    op.create_table(
        "portfolio_allocations",
        sa.Column("id", sa.Uuid(), nullable=False, default=sa.text("gen_random_uuid()")),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("weight", sa.Numeric(8, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["portfolio_id"], ["portfolios.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["graduation_candidates.id"]),
        sa.UniqueConstraint("portfolio_id", "candidate_id", name="uq_portfolio_candidate"),
    )
    op.create_index("ix_portfolio_allocations_portfolio_id", "portfolio_allocations", ["portfolio_id"])
    op.create_index("ix_portfolio_allocations_candidate_id", "portfolio_allocations", ["candidate_id"])

    # 8. portfolio_runs
    op.create_table(
        "portfolio_runs",
        sa.Column("id", sa.Uuid(), nullable=False, default=sa.text("gen_random_uuid()")),
        sa.Column("portfolio_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("metrics", postgresql.JSONB(), nullable=True),
        sa.Column("report_path", sa.String(512), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolios.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_portfolio_runs_portfolio_id", "portfolio_runs", ["portfolio_id"])
    op.create_index("ix_portfolio_runs_created_by", "portfolio_runs", ["created_by"])


def downgrade() -> None:
    op.drop_table("portfolio_runs")
    op.drop_table("portfolio_allocations")
    op.drop_table("portfolios")
    op.drop_table("graduation_stage_transitions")
    op.drop_table("graduation_candidates")
    op.drop_table("research_trials")
    op.drop_table("research_jobs")
    op.drop_table("asset_universe")
