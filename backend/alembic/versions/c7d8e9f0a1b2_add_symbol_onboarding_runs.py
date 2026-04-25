"""add symbol_onboarding_runs table

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-04-24 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "c7d8e9f0a1b2"
down_revision = "b6c7d8e9f0a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "symbol_onboarding_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("watchlist_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("symbol_states", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "request_live_qualification",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("job_id_digest", sa.String(64), nullable=False),
        sa.Column("cost_ceiling_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(12, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','completed','completed_with_failures','failed')",
            name="ck_symbol_onboarding_runs_status",
        ),
        sa.CheckConstraint(
            "cost_ceiling_usd IS NULL OR cost_ceiling_usd >= 0",
            name="ck_symbol_onboarding_runs_cost_ceiling_nonneg",
        ),
    )
    op.create_index(
        "ix_symbol_onboarding_runs_watchlist_name",
        "symbol_onboarding_runs",
        ["watchlist_name"],
    )
    op.create_index(
        "ix_symbol_onboarding_runs_created_at",
        "symbol_onboarding_runs",
        ["created_at"],
    )
    op.create_index(
        "ix_symbol_onboarding_runs_job_id_digest",
        "symbol_onboarding_runs",
        ["job_id_digest"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_symbol_onboarding_runs_job_id_digest",
        table_name="symbol_onboarding_runs",
    )
    op.drop_index(
        "ix_symbol_onboarding_runs_created_at",
        table_name="symbol_onboarding_runs",
    )
    op.drop_index(
        "ix_symbol_onboarding_runs_watchlist_name",
        table_name="symbol_onboarding_runs",
    )
    op.drop_table("symbol_onboarding_runs")
