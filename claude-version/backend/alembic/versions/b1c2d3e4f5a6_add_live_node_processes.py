"""add live_node_processes table

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-04-07 12:00:00.000000

Adds the per-restart lifecycle table used by the live-supervisor to track
trading subprocesses (Phase 1 task 1.1 of the Nautilus production hardening
plan).

Key design points enforced at the DB layer:

- ``pid`` is NULLABLE because the supervisor inserts the row before
  ``process.start()`` returns (Codex v3 P1 fix). The subprocess self-writes
  its own pid as its first DB action (Codex v5 P0 fix in Phase 1 task 1.8).
- The status enum includes ``building`` (written by the subprocess during
  ``node.build()`` per decision #17 v7 heartbeat-before-build).
- A partial unique index on ``(deployment_id)`` WHERE the status is in the
  active set (``starting``, ``building``, ``ready``, ``running``, ``stopping``)
  enforces the idempotency invariant that a deployment can have AT MOST ONE
  active process at any time (decision #13 database layer). The ``stopping``
  status is included so a start-during-stop race is blocked at the DB layer
  (Codex v4 P0 fix).
- ``failure_kind`` is a structured enum value (``FailureKind`` StrEnum stored
  as a ``String(32)``) populated by every failure writer so the
  ``/api/v1/live/start`` endpoint can classify outcomes without parsing
  ``error_message`` strings (Codex v7 P1 fix in Phase 1 task 1.14).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_node_processes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        # pid is NULLABLE — populated by the subprocess self-write after
        # process.start() runs (Codex v3 P1; Codex v5 P0).
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        # status: starting | building | ready | running | stopping | stopped | failed
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # failure_kind: FailureKind StrEnum value — none / halt_active /
        # spawn_failed_permanent / reconciliation_failed / build_timeout /
        # api_poll_timeout / in_flight / body_mismatch / unknown.
        sa.Column("failure_kind", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["deployment_id"], ["live_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_live_node_processes_deployment_id"),
        "live_node_processes",
        ["deployment_id"],
        unique=False,
    )
    # Idempotency layer (decision #13): a deployment can have at most ONE
    # active row at any time. The active set includes 'stopping' so a
    # start-during-stop race is blocked (Codex v4 P0).
    op.create_index(
        "uq_live_node_processes_active_deployment",
        "live_node_processes",
        ["deployment_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('starting','building','ready','running','stopping')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_live_node_processes_active_deployment",
        table_name="live_node_processes",
    )
    op.drop_index(
        op.f("ix_live_node_processes_deployment_id"),
        table_name="live_node_processes",
    )
    op.drop_table("live_node_processes")
