"""nullable user FKs on backtests/strategies/live_deployments

Revision ID: a1b2c3d4e5f6
Revises: 022772d92139
Create Date: 2026-04-06 19:30:00.000000

Allows API-key-authenticated writes to succeed without a pre-existing user
record. The created_by / started_by columns become nullable so requests
coming from the synthetic "api-key-user" don't violate NOT NULL when the
user row hasn't been created yet (e.g. first request before /auth/me).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "022772d92139"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("backtests", "created_by", nullable=True)
    op.alter_column("strategies", "created_by", nullable=True)
    op.alter_column("live_deployments", "started_by", nullable=True)


def downgrade() -> None:
    op.alter_column("live_deployments", "started_by", nullable=False)
    op.alter_column("strategies", "created_by", nullable=False)
    op.alter_column("backtests", "created_by", nullable=False)
