"""LiveDeployment model — STABLE logical record of a live or paper deployment.

A ``live_deployments`` row is the **stable, logical** record of a deployment.
It is uniquely keyed by ``identity_signature`` (a sha256 of the canonical-JSON
of the ``DeploymentIdentity`` tuple — see
``msai.services.live.deployment_identity``). Two deployments with the same
``identity_signature`` SHARE state across restarts (warm reload). Any
difference in any identity field produces a different signature → cold start
with isolated state.

Per-restart per-process state lives in :class:`msai.models.LiveNodeProcess`,
not here. The ``last_started_at`` / ``last_stopped_at`` columns are denormalized
"most recent run" timestamps for fast UI queries; the source of truth for any
specific run is the corresponding ``live_node_processes`` row.

Phase 1 task 1.1b adds the following columns to the v0 schema:

- ``deployment_slug``      — 16 hex chars, used to derive ``trader_id``
- ``identity_signature``   — sha256 of the identity tuple, UNIQUE
- ``trader_id``            — denormalized ``MSAI-{slug}`` for log queries
- ``strategy_id_full``     — denormalized ``{ClassName}-{slug}``
- ``account_id``           — IB account id (part of identity tuple)
- ``message_bus_stream``   — denormalized ``trader-MSAI-{slug}-stream``
- ``config_hash``          — sha256 of canonical-json strategy config
- ``instruments_signature`` — sorted, comma-joined canonical IDs
- ``last_started_at`` / ``last_stopped_at`` — most-recent-run timestamps
- ``startup_hard_timeout_s`` — per-deployment override for the watchdog ceiling

The OLD ``started_at`` / ``stopped_at`` columns are dropped — they tracked
only the FIRST start, but a deployment can be (re-)started many times.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — required at runtime for SQLAlchemy Mapped[]
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from msai.models.base import Base


class LiveDeployment(Base):
    """A live or paper-trading deployment of a strategy.

    A deployment is a STABLE logical record uniquely keyed by
    ``identity_signature``. Two deployments with the same signature share
    state across restarts (warm reload via Nautilus's Redis-backed cache
    + stable trader_id). Two with any different field have different
    signatures and start cold. Per-restart per-process state lives in
    ``live_node_processes`` (FK back to this row).
    """

    __tablename__ = "live_deployments"

    # ------------------------------------------------------------------
    # Pre-existing columns (from the v0 schema, unchanged in 1.1b)
    # ------------------------------------------------------------------
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[UUID] = mapped_column(
        ForeignKey("strategies.id"), index=True, nullable=False
    )
    strategy_code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    instruments: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="stopped")
    paper_trading: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    started_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # ------------------------------------------------------------------
    # Stable identity columns (Phase 1 task 1.1b — decision #7)
    # ------------------------------------------------------------------

    deployment_slug: Mapped[str] = mapped_column(
        String(16), nullable=False, unique=True, index=True
    )
    """16 hex chars (64 bits) — derived from ``secrets.token_hex(8)`` at
    first creation. Used to derive ``trader_id``, ``order_id_tag``, and
    the Nautilus message bus stream name. Stable across restarts of the
    same identity."""

    identity_signature: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    """sha256 hex of the canonical-JSON identity tuple. Includes
    ``started_by``, ``strategy_id``, ``strategy_code_hash``, ``config_hash``,
    ``account_id``, ``paper_trading``, ``instruments_signature``. The UNIQUE
    constraint enforces "warm restart on exact match, cold start on any
    change.\""""

    trader_id: Mapped[str] = mapped_column(String(32), nullable=False)
    """``f"MSAI-{deployment_slug}"`` — convenience denormalization for
    log queries. The Nautilus ``TraderId`` value for the live node."""

    strategy_id_full: Mapped[str] = mapped_column(String(280), nullable=False)
    """``f"{strategy_class_name}-{deployment_slug}"`` — the Nautilus
    ``StrategyId.value`` string. Used by Phase 4 state reload to find the
    persisted strategy state across restarts.

    Width must accommodate the full derived length: ``strategies.strategy_class``
    is VARCHAR(255), slug is 16 hex chars, plus the ``-`` separator = 272
    chars. 280 leaves small headroom (Codex Task 1.1b iteration 3, P1 fix)."""

    account_id: Mapped[str] = mapped_column(String(32), nullable=False)
    """IB account id (e.g. ``DU1234567`` for paper, ``U1234567`` for live).
    Also part of the identity tuple — switching accounts produces a new
    deployment row, not a warm restart on the existing one."""

    message_bus_stream: Mapped[str] = mapped_column(String(96), nullable=False)
    """``f"trader-MSAI-{deployment_slug}-stream"`` — the deterministic
    Redis Stream name where Nautilus publishes events for this trader
    (Phase 3 task 3.2 with ``stream_per_topic=False``). Persisted here so
    the projection consumer (3.4) knows what stream to subscribe to."""

    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """sha256 hex of the canonical JSON of the strategy config. Persisted
    for diagnostics — the source of truth is the identity tuple, but
    having the hash on the row makes log triage trivial."""

    instruments_signature: Mapped[str] = mapped_column(Text, nullable=False)
    """Sorted, comma-joined canonical instrument IDs. Same format as the
    identity tuple field, persisted for diagnostics.

    Stored as ``TEXT`` rather than ``VARCHAR(N)`` because a large options
    universe (30 underlyings × 4 expiries × 25 strikes ≈ 3000 IDs at
    ~20 chars each = ~60 KB) blows past any realistic fixed cap. The
    identity hash takes over uniqueness — this column is purely for
    human debugging (Codex Task 1.1b iteration 2, P2 fix)."""

    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Most recent ``/api/v1/live/start`` timestamp. Replaces the v0
    ``started_at`` column which only tracked the first start — a deployment
    can be (re-)started many times under the same logical identity."""

    last_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Most recent ``/api/v1/live/stop`` timestamp. Replaces the v0
    ``stopped_at`` column."""

    startup_hard_timeout_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Per-deployment override for the supervisor watchdog's hard
    wall-clock startup ceiling (Codex v7 P2). NULL falls back to the
    supervisor default (1800s in v8). Operators with large options
    universes (30+ underlyings, 10000+ strikes) can raise this per
    deployment. The watchdog's HEARTBEAT-based primary kill condition
    is independent of this value — a subprocess whose heartbeat thread
    keeps advancing is never killed regardless of this timeout. This is
    only the secondary "degenerate loop" backstop."""

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    strategy: Mapped[Strategy] = relationship(lazy="selectin")  # noqa: F821
    starter: Mapped[User] = relationship(lazy="selectin")  # noqa: F821

    # NOTE: no separate composite unique index — UNIQUE(identity_signature)
    # is the single source of identity truth. Decision #7.
