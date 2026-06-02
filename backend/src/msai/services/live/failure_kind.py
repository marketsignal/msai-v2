"""Structured failure classification for ``live_node_processes.failure_kind``.

Added in Phase 1 task 1.7 (plan v8 Codex v7 P1 fix). Replaces the v7
"parse error_message string" approach with a structured enum stored on
the row so the ``/api/v1/live/start`` endpoint can classify outcomes
without touching strings.

Writers:

- ``FleetRouter._mark_failed``  — phase-A/B/C failure paths
- ``FleetRouter.watchdog_loop`` — build-timeout SIGKILL
- ``FleetRouter._on_child_exit`` — reap-loop unexpected exit
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
    """The ``msai:risk:halt`` Redis flag was set when ``FleetRouter.spawn``
    re-checked it in phase B (decision #16, Codex v4 P0). The row is
    flipped to ``failed`` with this kind; the caller ACKs the command
    (no retry until ``/api/v1/live/resume`` clears the flag)."""

    ACCOUNT_HALT_ACTIVE = "account_halt_active"
    """The account-scoped halt latch ``msai:risk:halt:account:{account_id}``
    was set when ``FleetRouter.spawn`` re-checked it in phase B
    (PR 1 T8 / Codex iter 1 P2-1). Set by ``/api/v1/live/drain/{account_id}``
    and cleared independently of the fleet latch — other accounts
    under the same TWS login keep running. The row is flipped to
    ``failed`` with this kind; the caller ACKs the command (no retry
    until the drain latch is explicitly cleared)."""

    SPAWN_FAILED_PERMANENT = "spawn_failed_permanent"
    """A PRE-SPAWN / never-ran permanent failure: the subprocess failed to
    start (``mp.Process.start()`` raised), the payload factory raised a
    permanent (operator-config) error, or the subprocess crashed BEFORE it
    reached ``running`` (an engine/import/arm failure during
    ``kernel.start_async`` or in the outer catch-all, before
    ``_mark_running``). Permanent in the sense that a retry without an
    intervening fix will hit the same failure — the endpoint surfaces it to
    the caller as a terminal error, and the crash-recovery reaper / rescan
    must NOT auto-respawn it (see :meth:`is_recoverable_crash`): a node that
    never ran is an operator-START concern, not a runtime-crash to self-heal.

    NOT to be used for a node that RAN and then crashed — that is
    :attr:`NODE_CRASHED`, which the recovery paths DO re-drive."""

    NODE_CRASHED = "node_crashed"
    """A RUNTIME crash: the node RAN (reached ``running``/``ready``) and then
    its trading loop exited non-zero, OR the supervisor's reap loop observed a
    non-zero exit code for a node whose OS process had started. Written by
    ``FleetRouter._on_child_exit`` (generic non-zero exit on the stale-active
    path) and by ``_trading_node_subprocess`` when ``node.run_async()`` raises
    AFTER ``_mark_running``. This is the canonical RECOVERABLE-crash kind: the
    crash-recovery reaper auto-restart AND the periodic rescan re-drive it
    (bounded by the RestartPolicy ceiling) — distinguishing a genuine runtime
    crash to self-heal from a pre-spawn :attr:`SPAWN_FAILED_PERMANENT` that
    never ran (PR 2 / F2: resolve the SPAWN_FAILED_PERMANENT overload). The
    endpoint treats it as a permanent outcome for the originating ``/start``
    request (cacheable 503 — that start attempt did end in a crash), same HTTP
    shape as ``SPAWN_FAILED_PERMANENT``."""

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
    exceeded. Written by ``FleetRouter._watchdog_kill_one``."""

    RECONCILIATION_FAILED = "reconciliation_failed"
    """The subprocess raised ``StartupHealthCheckFailed`` after
    ``node.start_async()`` because ``kernel.trader.is_running`` never
    flipped to True within the timeout — the closest structured match
    to "engine connect or reconciliation or portfolio init failed"
    (the subprocess can't distinguish the three without reading
    internal Nautilus state; the full diagnosis is in
    ``error_message``). Written by ``_trading_node_subprocess`` in
    its ``finally`` block on catching ``StartupHealthCheckFailed``."""

    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    """The post-startup heartbeat sweep observed a row whose
    ``last_heartbeat_at`` is older than the stale threshold. Written
    by ``HeartbeatMonitor._mark_stale_as_failed`` when the subprocess
    silently died or got wedged (SIGSTOP'd, deadlocked, OS-killed)
    without writing its own terminal row. Previously used
    :attr:`UNKNOWN` which was indistinguishable on the HTTP layer
    from truly unclassified failures — operators couldn't tell a
    heartbeat-stuck subprocess from a garbage-column row. This kind
    is permanent from the endpoint's perspective: retries won't fix
    it automatically, the operator must restart the supervisor or
    the deployment."""

    REGISTRY_MISS = "registry_miss"
    """``lookup_for_live`` raised ``RegistryMissError`` — one or more
    symbols lack an active registry alias. Permanent; operator must run
    ``msai instruments refresh --symbols <X>`` before retrying. Endpoint
    maps this to HTTP 422 code=REGISTRY_MISS."""

    REGISTRY_INCOMPLETE = "registry_incomplete"
    """``lookup_for_live`` raised ``RegistryIncompleteError`` — a matched
    row has NULL/malformed required fields. Data-integrity issue; fire
    ERROR alert. Endpoint maps to HTTP 422 code=REGISTRY_INCOMPLETE."""

    UNSUPPORTED_ASSET_CLASS = "unsupported_asset_class"
    """``lookup_for_live`` raised ``UnsupportedAssetClassError`` — a row
    resolved to ``option`` or ``crypto``. Endpoint maps to HTTP 422
    code=UNSUPPORTED_ASSET_CLASS."""

    AMBIGUOUS_REGISTRY = "ambiguous_registry"
    """``lookup_for_live`` raised ``AmbiguousRegistryError`` — a bare
    symbol matches multiple registry rows across asset_classes, OR
    multiple active aliases share the same ``effective_from`` (operator-
    seeded overlap). Endpoint maps to HTTP 422 code=AMBIGUOUS_REGISTRY."""

    UNKNOWN = "unknown"
    """Fallback for rows whose ``failure_kind`` column is NULL or
    carries a value not in this enum. Used by :meth:`parse_or_unknown`
    when reading unrecognized historical values back. Writers MUST
    use a specific kind (``HEARTBEAT_TIMEOUT``, ``BUILD_TIMEOUT``,
    etc.) rather than ``UNKNOWN`` — a new path that can only surface
    as UNKNOWN is a writing bug to fix, not an acceptable state."""

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

    def is_retry_eligible(self) -> bool:
        """Whether a row in this terminal kind should let a REDELIVERED START
        re-spawn (rather than be ACK-dropped as a stale START).

        ``True`` ONLY for genuinely-transient failures whose dependency
        recovers on its own — the no-ACK redelivery path
        (:attr:`SPAWN_FAILED_TRANSIENT`) promises that retry. Every other
        terminal kind is permanent-or-deliberate from the redelivery's
        perspective and must NOT auto-respawn:

        * :attr:`SPAWN_FAILED_PERMANENT`, :attr:`NODE_CRASHED`,
          :attr:`RECONCILIATION_FAILED`, :attr:`BUILD_TIMEOUT`,
          :attr:`HEARTBEAT_TIMEOUT`, the registry kinds — operator-config /
          engine bugs / runtime crashes; a BARE PEL redelivery must not
          respawn them (a runtime crash is recovered by the reaper / rescan,
          NOT by the stale-START redelivery path — see
          :meth:`is_recoverable_crash`).
        * :attr:`HALT_ACTIVE` / :attr:`ACCOUNT_HALT_ACTIVE` — the operator
          halted/drained; respawning would defeat the kill switch.
        * :attr:`NONE` — a clean stop, not a failure.
        * :attr:`UNKNOWN` — fail-closed; an unrecognised value must not
          auto-respawn a real-money node.

        Used by ``FleetRouter._phase_a_reserve_slot`` (Codex P2): a START
        redelivered after a transient spawn failure (Postgres/Redis/import
        blip) finds the deployment flipped to ``failed`` with this kind, and
        must proceed to re-spawn — NOT be dropped by the terminal-deployment
        stale-START guard, which would silently break the retry the no-ACK
        path promised.
        """
        return self is FailureKind.SPAWN_FAILED_TRANSIENT

    def is_recoverable_crash(self) -> bool:
        """Whether the crash-recovery paths (reaper auto-restart + periodic
        rescan) should RE-DRIVE a terminal row carrying this ``failure_kind``.

        This is the ONE recovery-eligibility predicate shared by BOTH the
        reaper (``FleetRouter._on_child_exit``) and the periodic rescan
        (``FleetRouter._load_rescan_candidates`` / ``rescan_for_restart``) so
        the two agree (PR 2 / F2). It encodes the GOVERNING PRINCIPLE: the
        crash-recovery mechanisms re-drive a deployment ONLY when a node
        ACTUALLY RAN and was then LOST (a genuine runtime crash) OR an
        in-flight start was orphaned — NEVER a pre-spawn START failure that
        never ran.

        ``False`` (NOT recoverable — a pre-spawn / never-ran / deliberate
        terminal state the OPERATOR must clear by fixing config or re-issuing
        the start):

        * :attr:`SPAWN_FAILED_PERMANENT` — the node NEVER ran (``process.start()``
          raised, a permanent payload-config error, or a pre-``_mark_running``
          engine/import/arm failure). Re-driving churns a permanent error up to
          the ceiling. The operator fixes config and re-issues the start.
        * The registry kinds (:attr:`REGISTRY_MISS`, :attr:`REGISTRY_INCOMPLETE`,
          :attr:`UNSUPPORTED_ASSET_CLASS`, :attr:`AMBIGUOUS_REGISTRY`) — pre-spawn
          registry-resolution errors; never ran. Operator runs ``msai
          instruments refresh`` (or fixes the alias) and re-issues the start.
        * :attr:`HALT_ACTIVE` / :attr:`ACCOUNT_HALT_ACTIVE` — a halt-blocked
          START that never ran. After the operator ``/resume``\\s the halt, the
          OPERATOR re-issues the start; the rescan must NOT auto-start it (PR 2
          / F2 — this is also what settles the prior iter-3 P3 on
          re-scan-after-/resume: it is now PRINCIPLED, not a known-limitation).
        * :attr:`NONE` — a clean stop, not a failure.
        * The endpoint-only kinds (:attr:`IN_FLIGHT`, :attr:`API_POLL_TIMEOUT`,
          :attr:`BODY_MISMATCH`) — never written to a DB row.

        ``True`` (RECOVERABLE — a node that ran-then-crashed, or an orphaned
        in-flight start the recovery path owns):

        * :attr:`NODE_CRASHED` — the canonical runtime crash.
        * :attr:`RECONCILIATION_FAILED` — the node got far enough to attempt
          reconciliation (it ran); a respawn (bounded by the ceiling) is the
          right self-heal, and the ceiling brakes a persistent failure.
        * :attr:`HEARTBEAT_TIMEOUT` — a node that RAN then went silent
          (SIGKILL/OOM/wedge) — the supervisor-was-down stale-active orphan the
          rescan exists to recover; the bounded policy still brakes a loop.
        * :attr:`BUILD_TIMEOUT` — a node whose OS process started and then
          stalled in build; an orphaned in-flight start, recoverable (ceiling-
          braked).
        * :attr:`SPAWN_FAILED_TRANSIENT` — a transient dependency blip; the
          periodic rescan is the documented backstop when the fast no-ACK PEL
          retry gives up.
        * :attr:`UNKNOWN` — a terminal ``failed`` row whose kind is NULL or
          unrecognised. Treated as RECOVERABLE: the genuine outage-window crash
          (a node that ``_mark_terminal``\\ed to ``failed`` with no observed
          kind) presents as ``failed`` + NULL kind, and US-2 self-heal must
          recover it. The RestartPolicy ceiling is the brake if it loops; the
          halt latch + ``stop_requested_at`` + ``auto_restart_paused``
          suppressors (checked alongside this predicate) cover the deliberate
          cases.
        """
        return self not in _PRE_SPAWN_NEVER_RAN_KINDS


_PRE_SPAWN_NEVER_RAN_KINDS: frozenset[FailureKind] = frozenset(
    {
        # Pre-spawn permanent (node NEVER ran): operator-START concerns.
        FailureKind.SPAWN_FAILED_PERMANENT,
        FailureKind.REGISTRY_MISS,
        FailureKind.REGISTRY_INCOMPLETE,
        FailureKind.UNSUPPORTED_ASSET_CLASS,
        FailureKind.AMBIGUOUS_REGISTRY,
        # Halt-blocked START (never ran): operator re-issues after /resume.
        FailureKind.HALT_ACTIVE,
        FailureKind.ACCOUNT_HALT_ACTIVE,
        # Clean stop is not a crash.
        FailureKind.NONE,
        # Endpoint-only kinds never appear on a DB row, but exclude them
        # defensively so an accidental write can never auto-respawn.
        FailureKind.IN_FLIGHT,
        FailureKind.API_POLL_TIMEOUT,
        FailureKind.BODY_MISMATCH,
    }
)
"""Kinds the crash-recovery reaper + rescan must NOT re-drive — see
:meth:`FailureKind.is_recoverable_crash`. Defined as an explicit EXCLUSION set
(rather than an inclusion set) so a newly-added kind, or a NULL/unrecognised
historical value (→ :attr:`FailureKind.UNKNOWN`), defaults to RECOVERABLE: a
genuine crash that fails to self-heal is bounded by the RestartPolicy ceiling,
whereas a never-recovered genuine crash silently leaves a real-money account
flat-and-unmonitored. Fail toward recovery, brake with the ceiling."""
