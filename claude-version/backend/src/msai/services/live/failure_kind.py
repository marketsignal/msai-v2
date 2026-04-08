"""Structured failure classification for ``live_node_processes.failure_kind``.

Added in Phase 1 task 1.7 (plan v8 Codex v7 P1 fix). Replaces the v7
"parse error_message string" approach with a structured enum stored on
the row so the ``/api/v1/live/start`` endpoint can classify outcomes
without touching strings.

Writers:

- ``ProcessManager._mark_failed``  — phase-A/B/C failure paths
- ``ProcessManager.watchdog_loop`` — build-timeout SIGKILL
- ``ProcessManager._on_child_exit`` — reap-loop unexpected exit
- ``HeartbeatMonitor._mark_stale_as_failed`` — post-startup stale sweep
- ``_trading_node_subprocess`` (Task 1.8) — clean exit and in-subprocess failures

Readers:

- ``/api/v1/live/start`` (Task 1.14) — uses the value to shape the
  ``EndpointOutcome`` returned to the caller (permanent failure vs.
  transient vs. denied).

Stored on the row as ``String(32)``. Callers that read should go
through :meth:`FailureKind.parse_or_unknown` to handle NULL and
unrecognized historical values safely.
"""

from __future__ import annotations

from enum import StrEnum


class FailureKind(StrEnum):
    """Structured reason a ``live_node_processes`` row reached a terminal state.

    Values stored as strings so the DB column is decoupled from the
    enum definition — old rows with unknown values parse to
    :attr:`UNKNOWN` via :meth:`parse_or_unknown`.
    """

    NONE = "none"
    """Clean exit (``exit_code == 0``). Written by the subprocess when
    ``node.run()`` returns normally and by ``_on_child_exit`` when the
    reap loop observes a zero exit code on a row whose ``failure_kind``
    is still NULL."""

    HALT_ACTIVE = "halt_active"
    """The ``msai:risk:halt`` Redis flag was set when ``ProcessManager.spawn``
    re-checked it in phase B (decision #16, Codex v4 P0). The row is
    flipped to ``failed`` with this kind; the caller ACKs the command
    (no retry until ``/api/v1/live/resume`` clears the flag)."""

    SPAWN_FAILED_PERMANENT = "spawn_failed_permanent"
    """The subprocess either failed to start (``mp.Process.start()`` raised),
    died before writing a more specific ``failure_kind`` in its own
    ``finally`` block, or the supervisor's reap loop observed a
    non-zero exit code. Permanent in the sense that a retry without
    an intervening fix will hit the same failure — the endpoint
    should surface it to the caller as a terminal error, not a
    transient to retry automatically."""

    SPAWN_FAILED_TRANSIENT = "spawn_failed_transient"
    """Payload factory raised a transient error — typically a
    SQLAlchemy OperationalError when Postgres is briefly down or a
    network/timeout error during module import. The row is marked
    failed with this kind BUT the command is NOT ACKed — the caller
    returns False so the PEL redelivers via XAUTOCLAIM once the
    dependency recovers (Codex iter5 P2). The endpoint should treat
    this as retryable, not a terminal failure."""

    BUILD_TIMEOUT = "build_timeout"
    """The supervisor watchdog SIGKILLed the subprocess because its
    heartbeat stalled during startup (``starting``/``building``
    status) OR the per-deployment hard wall-clock ceiling was
    exceeded. Written by ``ProcessManager._watchdog_kill_one``."""

    RECONCILIATION_FAILED = "reconciliation_failed"
    """The subprocess raised ``StartupHealthCheckFailed`` after
    ``node.start_async()`` because ``kernel.trader.is_running`` never
    flipped to True within the timeout — the closest structured match
    to "engine connect or reconciliation or portfolio init failed"
    (the subprocess can't distinguish the three without reading
    internal Nautilus state; the full diagnosis is in
    ``error_message``). Written by ``_trading_node_subprocess`` in
    its ``finally`` block on catching ``StartupHealthCheckFailed``."""

    UNKNOWN = "unknown"
    """Fallback for rows whose ``failure_kind`` column is NULL or
    carries a value not in this enum. Used by
    ``HeartbeatMonitor._mark_stale_as_failed`` for post-startup stale
    sweeps where the subprocess died without reporting why, AND by
    :meth:`parse_or_unknown` when reading unrecognized historical
    values back."""

    # ------------------------------------------------------------------
    # Endpoint-layer values (Task 1.14)
    #
    # These are NOT written to ``live_node_processes.failure_kind`` by
    # any writer — the DB column only ever sees the values above. They
    # live on :class:`EndpointOutcome.failure_kind` for the HTTP layer
    # so ``/api/v1/live/start`` can produce a single structured type
    # regardless of whether the failure came from the DB row (per-run
    # failure) or the endpoint (idempotency layer / poll timeout).
    # ------------------------------------------------------------------

    IN_FLIGHT = "in_flight"
    """Another request with the same ``Idempotency-Key`` is currently
    holding the reservation (SETNX succeeded elsewhere). The endpoint
    returns HTTP 425 Too Early — the caller can retry after the
    in-flight request finishes (at which point the key holds either
    a cached response or has been released)."""

    API_POLL_TIMEOUT = "api_poll_timeout"
    """``/api/v1/live/start`` waited the full ``api_poll_timeout_s``
    for the subprocess to reach ``ready`` or ``failed`` and neither
    happened. Maps to HTTP 504 — transient, cacheable=False, so
    retries can re-attempt."""

    BODY_MISMATCH = "body_mismatch"
    """Same ``Idempotency-Key`` reused with a different request body.
    Maps to HTTP 422, cacheable=False — the caller does NOT own the
    reservation slot, so caching this response would overwrite the
    original correct cached response at the same key."""

    @classmethod
    def parse_or_unknown(cls, value: str | None) -> FailureKind:
        """Safely convert a raw column value back into a ``FailureKind``.

        ``None`` and unrecognized strings both map to :attr:`UNKNOWN`
        rather than raising, so the endpoint classification path is
        robust to pre-v8 rows that were written before
        ``failure_kind`` existed.
        """
        if value is None:
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN
