"""add smoke flag to backtests

Revision ID: a5y6z7a8b9c0
Revises: a4w5x6y7z8a9
Create Date: 2026-05-12

Adds a ``smoke`` boolean column to ``backtests`` to tag rows created by
the deploy-time data-path smoke (``deploy-on-vm.sh`` Phase 12). The
``GET /api/v1/backtests/history`` endpoint filters ``smoke=False`` by
default so operators don't see deploy-internal rows in their history.
The deploy rollback path deletes ``smoke=True`` rows tagged after the
deploy start timestamp, so failed deploys don't leak smoke artifacts
into prod state.

Chains onto ``aa00b11c22d3`` — the actual tip of the chain prior to
this PR (the ``alembic_version`` graph is linear; my first cut of this
migration accidentally introduced a second head by chaining from
``z4x5y6z7a8b9`` which already had ``a5b6c7d8e9f0`` as a child).

Migration shape: additive only (per ``rules/database.md``):

- ADD COLUMN ``smoke BOOLEAN NOT NULL DEFAULT false`` — Postgres 16
  metadata-only on existing tables, no rewrite required.
- ADD INDEX ``ix_backtests_smoke`` — supports the
  ``WHERE smoke = false`` filter in the history endpoint without a
  full scan once the table is large.

Rollback safety: forward-compatible — old code ignores the new column.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a5y6z7a8b9c0"
down_revision = "aa00b11c22d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backtests",
        sa.Column(
            "smoke",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_backtests_smoke",
        "backtests",
        ["smoke"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_backtests_smoke", table_name="backtests")
    op.drop_column("backtests", "smoke")
