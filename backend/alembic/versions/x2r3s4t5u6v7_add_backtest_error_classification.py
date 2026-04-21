"""add backtest error classification columns

Revision ID: x2r3s4t5u6v7
Revises: w1r2s3t4u5v6
Create Date: 2026-04-20 22:00:00.000000

NO SQL backfill of error_public_message from error_message. The raw column
can carry /app/... paths, JWT fragments, stack traces — putting that into
error_public_message without the sanitizer would leak through the API.
_build_error_envelope (Task B8) sanitizes-on-read when
error_public_message IS NULL but error_message is populated.

Rebased on w1r2s3t4u5v6 (PR #38 strategy-config-schema-extraction) after
that PR merged to main 2026-04-21. Original down_revision was
v0q1r2s3t4u5; both migrations touch different tables so the rebase is
mechanical (no data-dependency between them).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "x2r3s4t5u6v7"
down_revision: str = "w1r2s3t4u5v6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Postgres 16 add-column-with-default is a catalog-only op
    # (attmissingval fast path) so a NOT NULL + DEFAULT add is safe
    # even on a populated table. Research brief §4.
    op.add_column(
        "backtests",
        sa.Column(
            "error_code",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "backtests",
        sa.Column("error_public_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "backtests",
        sa.Column("error_suggested_action", sa.Text(), nullable=True),
    )
    op.add_column(
        "backtests",
        sa.Column(
            "error_remediation",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("backtests", "error_remediation")
    op.drop_column("backtests", "error_suggested_action")
    op.drop_column("backtests", "error_public_message")
    op.drop_column("backtests", "error_code")
