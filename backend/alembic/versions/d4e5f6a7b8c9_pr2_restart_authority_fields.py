"""add restart-authority columns to live_node_processes

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-31

PR 2 of the multi-account broker fleet introduces a bounded auto-restart
policy for per-account supervisor processes. The policy's state is persisted
on the ``live_node_processes`` row (not held in supervisor memory) so the
backoff schedule + max-attempts ceiling survive a container recreate. This
migration (T1) adds the schema; the policy logic lands in a later task.

Four ADDITIVE columns:

- ``auto_restart_paused``           — BOOLEAN NOT NULL DEFAULT false
- ``auto_restart_pause_reason``     — TEXT NULL
- ``consecutive_respawn_failures``  — INTEGER NOT NULL DEFAULT 0
- ``last_restart_at``               — TIMESTAMPTZ NULL

There is NO ``owner_generation`` / fencing column — this design has no
fencing.

Additive-only per ``.claude/rules/database.md``: the deploy pipeline rolls
back image SHAs but NOT schema, so old code from the prior release must
tolerate the newer schema after a rollback. The two NOT-NULL columns carry
server-side defaults (``false`` / ``0``) so old code's INSERTs — which never
name these columns — still succeed. On Postgres 16 adding a column with a
constant default is metadata-only (no table rewrite, no backfill). The
pre-existing ``gateway_session_key`` column and every other column are
untouched.

``downgrade()`` drops exactly the four added columns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "live_node_processes"


def upgrade() -> None:
    """Add 4 additive restart-authority columns to live_node_processes."""
    op.add_column(
        _TABLE,
        sa.Column(
            "auto_restart_paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("auto_restart_pause_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "consecutive_respawn_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("last_restart_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "last_restart_at")
    op.drop_column(_TABLE, "consecutive_respawn_failures")
    op.drop_column(_TABLE, "auto_restart_pause_reason")
    op.drop_column(_TABLE, "auto_restart_paused")
