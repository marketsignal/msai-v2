"""LiveNodeProcess model — per-restart process lifecycle for a live deployment.

A ``live_deployments`` row is the **logical** record of a deployment (stable
across restarts, keyed by ``identity_signature`` — see Phase 1 task 1.1b).
A ``live_node_processes`` row is the **per-restart run** record: it captures
a single spawn of a trading subprocess, including the pid, heartbeat, and
terminal outcome.

Key design points (from the hardening plan's Phase 1 task 1.1):

- ``pid`` is NULLABLE because the supervisor inserts the row with
  ``status='starting'`` BEFORE ``process.start()`` returns a real pid.
  The subprocess self-writes its own pid as its first DB action (decision
  from Phase 1 task 1.8 v6 / Codex v5 P0 fix).
- The status enum includes ``building`` (written by the subprocess during
  ``node.build()`` per decision #17 v7 heartbeat-before-build).
- A partial unique index on ``(deployment_id)`` WHERE the status is in the
  active set (``starting``, ``building``, ``ready``, ``running``, ``stopping``)
  enforces the idempotency invariant that a deployment can have AT MOST ONE
  active process at any time. This is the database layer of the three-layer
  idempotency model in decision #13.
- ``failure_kind`` is a structured enum value (``FailureKind`` StrEnum from
  ``services.live.idempotency``) stored as a ``String(32)``. The column is
  nullable for happy-path rows. All failure writers populate it so the
  ``/api/v1/live/start`` endpoint can classify outcomes without parsing
  ``error_message`` strings (decision from Phase 1 task 1.14 v7).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — required at runtime for SQLAlchemy Mapped[]
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from msai.models.base import Base, TimestampMixin


class LiveNodeProcess(Base, TimestampMixin):
    """Per-restart lifecycle record for a live deployment's trading subprocess."""

    __tablename__ = "live_node_processes"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    deployment_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("live_deployments.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # pid is NULLABLE — the supervisor INSERTs the row before process.start()
    # returns (Codex v3 P1 fix). The subprocess self-writes its own pid as
    # its first DB action (Codex v5 P0). In the phase-C-failure path, the
    # supervisor watchdog consults self._handles as a fallback pid source
    # (v9 Codex v8 P0 fix).
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)

    host: Mapped[str] = mapped_column(String(255), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Status values (see the hardening plan's state machine):
    #   starting  — row inserted by ProcessManager.spawn, pid not yet populated
    #   building  — subprocess is inside node.build(); heartbeat is running
    #   ready     — kernel.trader.is_running == True (canonical FSM signal)
    #   running   — node.run() loop active
    #   stopping  — SIGTERM sent, waiting for graceful exit
    #   stopped   — clean exit (terminal, exit_code=0)
    #   failed    — any failure path (terminal, exit_code != 0 OR None)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # failure_kind is a FailureKind StrEnum value (see
    # msai.services.live.idempotency). Stored as a string so the column
    # doesn't depend on the Python enum definition. The endpoint reads
    # this via FailureKind.parse_or_unknown() which handles NULL and
    # unrecognized values safely.
    failure_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        # Idempotency layer (decision #13): a deployment can have at most
        # ONE active row at any time. Two concurrent spawns racing on the
        # same deployment_id will fail at the database with a uniqueness
        # violation, which the supervisor catches and treats as "already
        # active, ACK the command."
        #
        # The active set includes 'stopping' (Codex v4 P0) so a
        # start-during-stop attempt is blocked at the DB layer.
        Index(
            "uq_live_node_processes_active_deployment",
            "deployment_id",
            unique=True,
            postgresql_where=text("status IN ('starting','building','ready','running','stopping')"),
        ),
    )
