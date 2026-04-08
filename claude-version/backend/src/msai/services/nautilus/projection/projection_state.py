"""In-memory rolling projection state (Phase 3 task 3.4).

Each FastAPI uvicorn worker keeps its own ``ProjectionState``
instance and updates it from two sources:

1. The :class:`StateApplier` task subscribes to the
   ``msai:live:state:*`` Redis pub/sub channels and feeds every
   event into ``apply()`` so the worker stays current with the
   live deployments.
2. :class:`PositionReader` (Task 3.5) cold-reads from the
   Nautilus ``Cache`` on the first query for a deployment and
   hydrates the state with whatever the cache returns at that
   moment.

The state is intentionally simple — a nested dict keyed by
``deployment_id`` → ``instrument_id`` → :class:`PositionSnapshot`
plus a flat ``account_id`` → :class:`AccountStateUpdate` mapping
plus a ``deployment_id`` → ``RiskHaltEvent`` mapping for halts
plus a ``deployment_id`` → ``DeploymentStatusEvent`` mapping for
lifecycle status.

We do NOT keep a fill or order-status history — those events
are streamed verbatim to WebSocket clients via the events
channel and the audit table is the persistent record. The
projection state is for "current state" reads only, not
history.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from msai.services.nautilus.projection.events import (
    AccountStateUpdate,
    DeploymentStatusEvent,
    FillEvent,
    OrderStatusChange,
    PositionSnapshot,
    RiskHaltEvent,
)

# Sentinel for the ``account`` keyword on
# :meth:`ProjectionState.hydrate_from_cold_read`. We can't use
# ``None`` as the default because ``None`` is a VALID hydrated
# account value ("the cold read found no account"). Codex
# batch 8 P1 fix.
_UNSET = object()

if TYPE_CHECKING:
    from uuid import UUID

    from msai.services.nautilus.projection.events import InternalEvent  # noqa: F401


class ProjectionState:
    """Per-worker in-memory rolling state of every active
    deployment.

    Thread-safe via a single ``threading.RLock``. The lock is
    cheap because every read / write is a single dict lookup
    or assignment — no I/O, no nested calls. The async
    StateApplier task and synchronous PositionReader read
    paths share the same instance.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._positions: dict[UUID, dict[str, PositionSnapshot]] = {}
        """deployment_id → instrument_id → latest snapshot.

        Membership in this dict (even pointing to an empty dict)
        means "positions are hydrated" — the cold path will not
        re-read Redis for this deployment. See
        :meth:`is_positions_hydrated`."""

        self._accounts: dict[UUID, AccountStateUpdate | None] = {}
        """deployment_id → latest account snapshot, or ``None``
        if the cold read found no account. The presence of a
        key (even with a ``None`` value) means "account is
        hydrated" — see :meth:`is_account_hydrated`."""

        self._halts: dict[UUID, RiskHaltEvent] = {}
        """deployment_id → halt event (presence means halted)."""

        self._statuses: dict[UUID, DeploymentStatusEvent] = {}
        """deployment_id → latest lifecycle status."""

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply(self, event: InternalEvent) -> None:
        """Dispatch an internal event into the appropriate state
        slot. Called by:

        - :class:`StateApplier` background task on every
          pub/sub message.
        - :class:`PositionReader` cold-read hydration path
          (via :meth:`hydrate_from_cold_read`).

        Unknown event types raise — the schema is closed and
        any drift should be loud.
        """
        with self._lock:
            if isinstance(event, PositionSnapshot):
                self._apply_position(event)
            elif isinstance(event, AccountStateUpdate):
                self._accounts[event.deployment_id] = event
            elif isinstance(event, RiskHaltEvent):
                self._halts[event.deployment_id] = event
            elif isinstance(event, DeploymentStatusEvent):
                self._statuses[event.deployment_id] = event
            elif isinstance(event, (FillEvent, OrderStatusChange)):
                # Fills + order-status events are pure passthroughs to
                # the WebSocket layer and the audit table — they don't
                # affect rolling state. Drop here so the StateApplier
                # doesn't error on them.
                #
                # IMPORTANT (Codex v6 P1): we do NOT mark the
                # deployment as "positions hydrated" here. A
                # FillEvent flipping the hydration flag without
                # populating positions would cause the fast path
                # to return [] when real positions exist in
                # Redis.
                return
            else:
                raise TypeError(f"unknown projection event type: {type(event).__name__}")

    def _apply_position(self, event: PositionSnapshot) -> None:
        """Insert / replace the snapshot for one
        ``(deployment_id, instrument_id)`` pair. A flat-zero
        snapshot still replaces the previous one — closed
        positions stay in the state with ``qty=0`` so the UI
        can show the realized P&L.

        Inserting any position implicitly marks the deployment
        as ``positions_hydrated`` because the deployment_id key
        is now present in ``self._positions``. The cold path
        will not re-read Redis for this deployment until
        :meth:`forget` is called.
        """
        instruments = self._positions.setdefault(event.deployment_id, {})
        instruments[event.instrument_id] = event

    # ------------------------------------------------------------------
    # Hydration tracking (per-domain)
    # ------------------------------------------------------------------

    def is_positions_hydrated(self, deployment_id: UUID) -> bool:
        """True if positions have been written for this
        deployment by EITHER the StateApplier (via a
        ``PositionSnapshot`` event) OR the PositionReader cold
        path (via :meth:`hydrate_from_cold_read`). The fast
        path in PositionReader gates on this — once True, we
        never re-read Redis until :meth:`forget` clears the
        deployment."""
        with self._lock:
            return deployment_id in self._positions

    def is_account_hydrated(self, deployment_id: UUID) -> bool:
        """True if the account snapshot has been written for
        this deployment by EITHER the StateApplier (via an
        ``AccountStateUpdate`` event) OR the PositionReader
        cold path. ``None`` is a valid hydrated value (the
        cold read found no account)."""
        with self._lock:
            return deployment_id in self._accounts

    def hydrate_from_cold_read(
        self,
        deployment_id: UUID,
        *,
        positions: list[PositionSnapshot] | None = None,
        account: AccountStateUpdate | None | object = _UNSET,
    ) -> None:
        """Write a cold-read result into the rolling state.
        Called by :class:`PositionReader` after the ephemeral
        Cache returns. Per-domain only-if-still-cold semantics
        (Codex v7 P1): if the StateApplier has raced us between
        the caller's fast-path check and this hydrate call, we
        leave the existing fresher data alone.

        ``positions`` of ``None`` means "skip the positions
        domain"; an empty list means "the cold read found zero
        positions" — the latter flips
        :meth:`is_positions_hydrated` to True so subsequent
        reads serve ``[]`` from the fast path.

        ``account`` uses an explicit sentinel because ``None``
        is a valid hydrated value ("the cold read found no
        account") — Codex batch 8 P1 fix. Omit the argument to
        skip the account domain; pass ``None`` to record "no
        account found" so the next call serves ``None`` from
        the fast path instead of cold-reading again.
        """
        with self._lock:
            if positions is not None and deployment_id not in self._positions:
                instruments: dict[str, PositionSnapshot] = {}
                for snapshot in positions:
                    instruments[snapshot.instrument_id] = snapshot
                self._positions[deployment_id] = instruments
            if account is not _UNSET and deployment_id not in self._accounts:
                # account is either an AccountStateUpdate or
                # explicitly None — both are valid hydrated
                # values. Cast away the sentinel union for
                # mypy's benefit.
                self._accounts[deployment_id] = account  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get_positions(self, deployment_id: UUID) -> dict[str, PositionSnapshot]:
        """Return a SHALLOW COPY of the position dict for one
        deployment. Returning a copy keeps callers from racing
        the StateApplier on subsequent reads — the snapshot is
        a frozen Pydantic model so the values can be shared."""
        with self._lock:
            return dict(self._positions.get(deployment_id, {}))

    def get_position(self, deployment_id: UUID, instrument_id: str) -> PositionSnapshot | None:
        with self._lock:
            return self._positions.get(deployment_id, {}).get(instrument_id)

    def positions(self, deployment_id: UUID) -> list[PositionSnapshot]:
        """List form of :meth:`get_positions` for the
        ``PositionReader`` API. Returns the OPEN positions
        only — closed positions (``qty == 0``) are filtered
        out so the fast path matches the cold path's
        ``cache.positions_open()`` behavior (Codex batch 8 P1
        fix). Returns ``[]`` for an unknown OR
        hydrated-but-empty deployment — the caller checks
        :meth:`is_positions_hydrated` first to distinguish."""
        with self._lock:
            return [
                snapshot
                for snapshot in self._positions.get(deployment_id, {}).values()
                if snapshot.qty != 0
            ]

    def get_account(self, deployment_id: UUID) -> AccountStateUpdate | None:
        with self._lock:
            return self._accounts.get(deployment_id)

    def account(self, deployment_id: UUID) -> AccountStateUpdate | None:
        """Alias of :meth:`get_account` matching the
        ``PositionReader`` naming convention."""
        with self._lock:
            return self._accounts.get(deployment_id)

    def get_halt(self, deployment_id: UUID) -> RiskHaltEvent | None:
        with self._lock:
            return self._halts.get(deployment_id)

    def is_halted(self, deployment_id: UUID) -> bool:
        with self._lock:
            return deployment_id in self._halts

    def get_status(self, deployment_id: UUID) -> DeploymentStatusEvent | None:
        with self._lock:
            return self._statuses.get(deployment_id)

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def has_deployment(self, deployment_id: UUID) -> bool:
        """True if we have ANY state for this deployment.
        Used by :class:`PositionReader` to decide whether a
        cold-read against the Nautilus cache is needed."""
        with self._lock:
            return (
                deployment_id in self._positions
                or deployment_id in self._accounts
                or deployment_id in self._statuses
                or deployment_id in self._halts
            )

    def forget(self, deployment_id: UUID) -> None:
        """Drop all state for a deployment. Called when the
        supervisor reports a terminal status (stopped/failed)
        AND a configurable retention window has elapsed.
        Phase 4 wires the eviction policy; for now this is a
        manual hook tests can call."""
        with self._lock:
            self._positions.pop(deployment_id, None)
            self._accounts.pop(deployment_id, None)
            self._halts.pop(deployment_id, None)
            self._statuses.pop(deployment_id, None)
