"""add reaper-authority columns (stop_requested_at + restart_dispatched_at)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-05-31

PR 2 / Task 6 — the COMMON-CRASH reaper fix (council #3 verdict, 2026-05-31).

The subprocess writes its OWN terminal ``failed`` row LAST in its ``finally``
("Terminal write LAST" in ``trading_node_subprocess.py``) before exiting, so
for the common crash the row is already terminal by the time the reaper
(``FleetRouter._on_child_exit``) sees the exit. The reaper now classifies the
latest terminal row and routes an eligible non-zero-exit crash into the bounded
auto-restart path. That classification needs two pieces of durable, node-scoped
state the prior schema didn't carry:

- ``stop_requested_at``     — TIMESTAMPTZ NULL. Set by the operator ``/stop``
  path (``FleetRouter.stop``) UNDER a ``FOR UPDATE`` row lock BEFORE the
  SIGTERM, so operator-stop intent survives the subprocess's terminal write.
  The reaper suppresses auto-restart whenever this is non-NULL — even on a
  non-zero exit (a ``/stop`` whose graceful shutdown then crashes must NOT be
  resurrected). The ``FOR UPDATE`` on both the ``/stop`` write and the reaper's
  classify read serialises the stop-vs-self-crash race.
- ``restart_dispatched_at`` — TIMESTAMPTZ NULL. The reaper's idempotency
  sentinel: set under the same ``FOR UPDATE`` BEFORE dispatching a restart, and
  the reaper skips dispatch if it is already set — so a duplicate reaper pass on
  the same still-``failed`` terminal row never re-dispatches.

Both are nullable; additive-only per ``.claude/rules/database.md`` (the deploy
pipeline rolls back image SHAs but NOT schema, so old code from the prior
release must tolerate the newer schema after a rollback — both columns are NULL
on old-code INSERTs, which never name them). On Postgres 16 adding a nullable
column with no default is metadata-only (no table rewrite). Every pre-existing
column — including the four T1 restart-authority columns and
``gateway_session_key`` — is untouched.

``downgrade()`` drops exactly these two columns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "live_node_processes"


def upgrade() -> None:
    """Add the two nullable reaper-authority columns to live_node_processes."""
    op.add_column(
        _TABLE,
        sa.Column("stop_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("restart_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "restart_dispatched_at")
    op.drop_column(_TABLE, "stop_requested_at")
