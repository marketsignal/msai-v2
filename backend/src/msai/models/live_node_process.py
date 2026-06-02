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
    Boolean,
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
    #   starting  — row inserted by FleetRouter.spawn, pid not yet populated
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

    gateway_session_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    """Stable identifier for the (IB Gateway host, port, TWS login)
    tuple this subprocess is bound to. Per-gateway-session spawn guard
    (PR #3) filters on this key. Added nullable in PR #1; populated by
    PR #2 and enforced NOT NULL in PR #3."""

    # --- Restart-authority state (PR #2, multi-account broker fleet) -------
    # These four columns back the bounded auto-restart policy for per-account
    # supervisor processes. They are persisted on the DB row (not held in
    # supervisor memory) so the backoff / max-attempts ceiling survives a
    # container recreate. T1 adds the schema; the policy logic lands later.
    #
    # The two NOT-NULL columns carry server-side defaults so old code (which
    # never names them on INSERT) keeps working after a rollback — additive-
    # only discipline (.claude/rules/database.md). No fencing / owner_generation
    # in this design.

    auto_restart_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    """When True, the reaper must NOT auto-respawn this process — the bounded
    restart policy has tripped (e.g. consecutive-failure ceiling reached) and
    an operator must intervene."""

    auto_restart_pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Operator-facing free-text reason auto-restart was paused (e.g.
    "max respawn ceiling reached"). NULL while auto-restart is active."""

    consecutive_respawn_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    """Count of consecutive failed respawn attempts for this logical process.
    Drives the backoff schedule and the pause-at-ceiling decision; reset to 0
    on a successful (healthy) restart."""

    last_restart_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Wall-clock timestamp of the most recent auto-restart attempt. NULL until
    the first restart. Used to compute backoff windows."""

    # --- Reaper-authority state (PR #2 / T6, council #3 verdict) -----------
    # The subprocess writes its OWN terminal row LAST (``_mark_terminal``,
    # "Terminal write LAST") before exiting, so for the common crash the row is
    # already ``failed`` by reap time. The reaper now classifies the latest
    # terminal row and routes an eligible crash into the bounded auto-restart
    # path. These two columns carry the durable, node-scoped intent + idempotency
    # state that classification needs across the subprocess's terminal write.
    # Both are nullable; additive-only (.claude/rules/database.md) — NULL on
    # old-code INSERTs after a rollback.

    stop_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Set by BOTH operator-``/stop`` writers under a ``FOR UPDATE`` row lock
    BEFORE the SIGTERM is sent (set-intent-before-signal): (1) the ``/stop`` API
    handler stamps it SYNCHRONOUSLY before publishing the STOP command — closing
    the publish→consume gap where a node could self-crash before the supervisor
    consumes the command and be wrongly auto-restarted (a plain ``/stop`` sets no
    halt latch, unlike ``/kill-all``//``/drain``); and (2) ``FleetRouter.stop``
    (the supervisor's consume handler) idempotently re-stamps it. Durable
    operator-stop intent that survives the subprocess's own terminal write: the
    reaper suppresses auto-restart whenever this is non-NULL, EVEN on a non-zero
    exit (a ``/stop`` whose graceful shutdown then crashes must NOT be
    resurrected); the startup re-scan honors it too. The ``FOR UPDATE`` on both
    writer and reader closes the stop-vs-self-crash race. NULL means no operator
    stop was requested."""

    restart_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """The reaper's idempotency sentinel. Set under the reaper's ``FOR UPDATE``
    row lock BEFORE dispatching an auto-restart; the reaper skips dispatch if it
    is already set — so a duplicate reaper pass on the same still-``failed``
    terminal row never re-dispatches (no double-spawn). NULL until the reaper
    first dispatches a restart against this row."""

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
