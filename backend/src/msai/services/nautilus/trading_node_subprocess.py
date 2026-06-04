"""Live trading subprocess entry point (Phase 1 task 1.8).

Runs in a fresh Python interpreter under the ``mp.get_context('spawn')``
context that :meth:`msai.live_supervisor.FleetRouter.spawn` creates.
Owns one Nautilus ``TradingNode`` from construction through clean
(or unclean) shutdown.

Decisions embedded here
-----------------------

- **Self-write pid + status='building' BEFORE anything Nautilus-side**
  (decision #17, Codex v5 P0). The supervisor's ``spawn`` path has a
  best-effort phase-C that also writes the pid, but a phase-C failure
  would leave ``live_node_processes.pid=NULL`` and break ``/stop``
  after a supervisor restart. Having the subprocess self-write makes
  ``pid`` populated on every code path.

- **Heartbeat thread starts BEFORE ``node.build()``** (decision #17).
  Hanging builds must age out via the HeartbeatMonitor / watchdog
  stale sweep — starting the heartbeat after ``build`` would defeat
  that.

- **No ``asyncio.wait_for`` around ``node.build()``** (Codex v5 P0).
  ``wait_for`` only cancels the awaiter, not the C-side thread that
  an IB contract load is blocked in. Wedged builds are killed from
  OUTSIDE by the supervisor's watchdog. Inside the subprocess,
  ``node.build()`` runs normally.

- **Canonical FSM signal** for "trader actually started" is
  ``node.kernel.trader.is_running`` (decision #14, see
  :mod:`msai.services.nautilus.startup_health`). Nautilus's engine
  methods silently early-return on failure, so a "succeeded" return
  from ``node.start_async()`` doesn't prove the trader is live. We
  poll ``is_running`` after start and raise
  :class:`StartupHealthCheckFailed` on timeout.

Testability
-----------

Production ``mp.Process`` can't easily be unit-tested with a real
Nautilus ``TradingNode`` because the IB adapter needs IB Gateway.
Instead, :func:`run_subprocess_async` takes a ``node_factory``
callable that constructs the node from the payload. The default
factory builds a real ``TradingNode`` via
:func:`build_live_trading_node_config` + ``TradingNode(config)``;
tests inject a fake factory that returns a stub with the right
method shape, so every correctness property (order-of-operations,
failure paths, cleanup) is exercised end-to-end without touching
Nautilus.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import signal
import sys
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, NoReturn
from uuid import UUID  # noqa: TC003 — used at runtime for dataclass field type

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from msai.models.live_node_process import LiveNodeProcess
from msai.services.live.failure_kind import FailureKind
from msai.services.nautilus.startup_health import (
    StartupHealthCheckFailed,
    wait_until_ready,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.services.nautilus.security_master.live_resolver import (
        ResolvedInstrument,
    )


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IB exec-side pacing/throttle detection (PR 1b T7)
# ---------------------------------------------------------------------------
#
# IB rejects orders with a throttle indication when the client exceeds the
# message rate. We surface that as a per-account metric so an operator can see
# pacing pressure before it becomes a halt. Discrimination matters: the numeric
# codes 100 / 162 / 420 appear ONLY as legacy MARKET-DATA pacing and must NOT
# be treated as exec-side throttle. We match exec-side throttle PHRASES instead,
# case-insensitively.

_IB_EXEC_PACING_PHRASES: tuple[str, ...] = (
    "pacing violation",
    "max rate of messages",
    "throttle",
)

# IB market-DATA pacing/rate codes. These are EXCLUDED — IB is exec-only in this
# system (Databento owns market data), so a data-pacing rejection must NOT touch
# the exec-pacing counter. The exclusion matters because IB's canonical message
# texts for these codes *embed the matched phrases verbatim* — e.g.
#   "Error 100: max rate of messages per second exceeded"
#   "Error 162: Historical Market Data Service error message: pacing violation"
#   "Error 420: ...pacing violation"
# so pure phrase matching would mis-count them. We extract a leading/embedded IB
# error code and suppress these BEFORE phrase matching.
_IB_MARKET_DATA_PACING_CODES: frozenset[int] = frozenset({100, 162, 420})

# Matches the IB error code in the reason text, anchored at the start (after
# optional leading whitespace) in any of the forms IB / Nautilus surface:
#   "Error 162: ..."  |  "error code 162: ..."  |  "162: ..."
# Anchoring at the start prevents an incidental number deeper in free text from
# being mistaken for the rejection's code.
_IB_REASON_CODE_RE: Final = re.compile(
    r"^\s*(?:error(?:\s+code)?\s+)?(\d+)\s*:",
    re.IGNORECASE,
)


def _extract_ib_error_code(reason: str) -> int | None:
    """Extract the leading IB error code from an OrderRejected *reason*, if present.

    Returns the integer code for the IB reason formats ``Error 162: ...`` /
    ``error code 162: ...`` / ``162: ...``; ``None`` when the reason carries no
    leading code (codeless exec-side throttles take this path)."""
    match = _IB_REASON_CODE_RE.match(reason)
    if match is None:
        return None
    return int(match.group(1))


def is_ib_exec_pacing_reason(reason: str | None) -> bool:
    """Return True if an OrderRejected *reason* indicates IB EXEC-side
    pacing/throttle (NOT the legacy market-data pacing codes 100/162/420).

    Discrimination: when the reason carries a leading IB error code that is a
    market-data pacing code (100/162/420), it is suppressed regardless of the
    phrases it contains — IB's canonical texts for those codes embed the
    exec-throttle phrases verbatim. Codeless reasons (and reasons with a
    non-market-data code) fall through to pure phrase matching, so exec-side
    throttles that carry no code still count."""
    if not reason:
        return False
    if _extract_ib_error_code(reason) in _IB_MARKET_DATA_PACING_CODES:
        return False
    lowered = reason.lower()
    return any(phrase in lowered for phrase in _IB_EXEC_PACING_PHRASES)


async def record_ib_exec_pacing(redis: Any, *, reason: str | None, account_id: str | None) -> None:
    """INCR the per-account IB exec-pacing counter when *reason* is a throttle.

    No-ops when the reason is not a pacing/throttle indication or when no
    account is known. Swallows Redis errors — a metrics counter must NEVER
    propagate into the engine-level audit hook (which would drop the audit
    write for an order rejection)."""
    if account_id is None or not is_ib_exec_pacing_reason(reason):
        return
    from msai.core.halt_keys import ib_exec_pacing_key

    try:
        await redis.incr(ib_exec_pacing_key(account_id))
    except Exception:  # noqa: BLE001 — metrics INCR must not break the audit hook
        log.warning("ib_exec_pacing_incr_failed", extra={"account_id": account_id})


# ---------------------------------------------------------------------------
# Node-crash child reaping (PR 2 T7) — os.setsid session detachment
# ---------------------------------------------------------------------------
#
# ``multiprocessing.Process`` has NO ``preexec_fn`` (that is a
# ``subprocess.Popen`` feature), so the session detachment must run INSIDE the
# spawned target — as the very first executable statement of
# :func:`_trading_node_subprocess`, before any ``nautilus_trader`` import opens
# sockets or a signal can arrive.
#
# ``os.setsid()`` puts the trading-node child in its own session / process group.
# Two payoffs:
#   1. A SIGTERM delivered to the SUPERVISOR's process group does not cascade
#      into the nodes (the supervisor controls each node's lifecycle explicitly
#      via the command bus + targeted SIGTERM, never a group signal).
#   2. When the supervisor exits, the child reparents cleanly to the container
#      init (PID 1) instead of being orphaned to whatever inherits the group.
#
# This pairs with ``init: true`` on the ``live-supervisor`` compose service: tini
# (the container PID 1) reaps the exited node children so no zombies accumulate.
# ``os.setsid`` only detaches the session; tini does the actual reaping.
#
# **Honest scope:** this hardens the NODE-crash + child-reaping case. It does NOT
# make a supervisor-PROCESS crash survivable — the supervisor IS PID 1, so its
# crash recreates the container. That survivability is the deferred per-account-
# container capability (see ``docs/decisions/multi-account-broker-fleet.md``).


def _detach_session() -> None:
    """Detach the spawned trading-node child into its own session/process group.

    Called as the FIRST executable statement of :func:`_trading_node_subprocess`
    (the ``mp.Process`` target). Idempotent + best-effort: if the child is
    already a session leader (``os.setsid`` raises ``PermissionError`` /
    ``OSError`` in that case) we log and continue — detachment is a hardening
    measure, never a reason to refuse to start a live node.
    """
    try:
        os.setsid()
    except OSError as exc:
        # Already a session/group leader (or setsid otherwise refused). The
        # process keeps its current session; this is a no-op hardening miss,
        # not a startup blocker.
        log.warning("node_child_setsid_skipped", extra={"error": repr(exc)})


# ---------------------------------------------------------------------------
# Node-side live-halt gate enforcement + wiring (F6 — PR 2 T2, REAL-MONEY P0)
# ---------------------------------------------------------------------------
#
# These two functions are module-level (not nested in the production wrapper) so
# the mandatory-base-class enforcement and the halt-refresh wiring are directly
# unit-testable without standing up a subprocess. They are invoked from the
# live ``on_post_build`` hook (``_wire_market_hours``), AFTER ``node.build()``
# (strategy CLASSES don't exist before build) but BEFORE ``node.run_async()``.

_GATED_SUBMIT_METHODS = ("submit_order", "submit_order_list", "modify_order")
"""The public submit API the halt gate covers. ``RiskAwareStrategy`` must be the
FIRST MRO owner of each so its gated override is the effective implementation."""

HALT_REFRESH_INTERVAL_S: float = 1.0
"""Background halt-cache refresh cadence (``≤1s`` — comfortably under the mixin's
``HALT_CACHE_MAX_AGE_S=2.0`` staleness ceiling)."""


def enforce_halt_gate_mro(strategies: list[Any]) -> None:
    """Reject any live strategy that is not halt-gated (F6 — fail-closed).

    For EACH strategy, require:

    1. ``isinstance(strategy, RiskAwareStrategy)`` — the mixin is present; AND
    2. for EACH gated submit method, the FIRST class in ``type(strategy).__mro__``
       whose ``__dict__`` contains that name is ``RiskAwareStrategy`` — i.e. the
       gated override actually WINS the MRO.

    ``isinstance`` ALONE is insufficient: ``class S(Strategy, RiskAwareStrategy)``
    (mixin LAST) passes ``isinstance`` but resolves ``self.submit_order`` to
    Nautilus's un-gated ``Strategy.submit_order``. And the weaker
    ``is not Strategy.submit_order`` identity check would accept a subclass that
    RE-OVERRIDES ``submit_order`` with its own un-gated impl. "First MRO owner is
    ``RiskAwareStrategy``" rejects BOTH.

    Raises a plain ``RuntimeError`` on any violation — the subprocess run loop's
    catch-all maps it to ``FailureKind.SPAWN_FAILED_PERMANENT`` and the node never
    reaches ``run_async()`` (a non-halt-aware live strategy never trades). We use
    an explicit ``raise`` (NOT ``assert`` — ``python -O`` strips asserts).
    """
    from msai.services.nautilus.risk import RiskAwareStrategy

    for strategy in strategies:
        cls = type(strategy)
        if not isinstance(strategy, RiskAwareStrategy):
            raise RuntimeError(
                f"live strategy {cls.__module__}.{cls.__qualname__} is not a "
                "RiskAwareStrategy — the node-side halt gate is mandatory for live "
                "trading. Declare it as `class X(RiskAwareStrategy, Strategy)` "
                "(mixin FIRST). SPAWN_FAILED_PERMANENT."
            )
        for method_name in _GATED_SUBMIT_METHODS:
            first_owner = next(
                (c for c in cls.__mro__ if method_name in c.__dict__),
                None,
            )
            if first_owner is not RiskAwareStrategy:
                raise RuntimeError(
                    f"live strategy {cls.__module__}.{cls.__qualname__} does not let "
                    f"RiskAwareStrategy own '{method_name}' (first MRO owner is "
                    f"{first_owner.__qualname__ if first_owner else None}). The halt "
                    "gate must be the EFFECTIVE implementation: put RiskAwareStrategy "
                    "FIRST in the base-class tuple and do NOT re-override the submit "
                    "methods. SPAWN_FAILED_PERMANENT."
                )


async def _read_halt_value(redis_client: Any, account_id: str) -> bool:
    """Return True iff the fleet OR the account halt latch is set.

    Reads ``fleet_halt_key()`` and ``account_halt_key(account_id)`` directly so
    the node-side gate is router-independent (it survives a supervisor outage).
    A non-empty value at EITHER key means halted.
    """
    from msai.core.halt_keys import account_halt_key, fleet_halt_key

    fleet = await redis_client.get(fleet_halt_key())
    if fleet:
        return True
    if account_id:
        account = await redis_client.get(account_halt_key(account_id))
        if account:
            return True
    return False


async def wire_halt_refresh(
    *,
    strategies: list[Any],
    redis_client: Any,
    account_id: str,
    interval_s: float = HALT_REFRESH_INTERVAL_S,
    audit_writer: Any | None = None,
    denial_contexts: dict[str, Any] | None = None,
) -> asyncio.Task[None]:
    """Arm the live halt gate on every ``RiskAwareStrategy`` + start the
    background halt-cache refresh task. Returns the task handle so the caller
    cancels it on shutdown.

    **FAIL-CLOSED arming order (P1 — the precise F6 hole).** The gate is armed
    and the background recovery task is created BEFORE the immediate refresh is
    attempted. The immediate refresh — which opens a fresh Redis client on the
    critical build window — can raise (connection-refused / timeout / DNS blip).
    If we armed AFTER the refresh, that error would leave the gate disarmed for
    the node's ENTIRE lifetime → ``_gate_allows`` takes the inert branch →
    admits ALL opening orders with ZERO node-side halt enforcement — exactly the
    supervisor-independent hole F6 exists to defend. So we arm FIRST: an armed
    gate with a ``None`` cache fails CLOSED (blocks opening orders, allows
    flatten), and the background task recovers the cache when Redis returns. The
    immediate refresh is then best-effort (it only closes the cold-start window
    where a healthy node would briefly false-block its first opening order).

    The ``audit_writer`` (an :class:`OrderAuditWriter`) is injected onto each
    gated strategy's ``_audit`` slot here so node-side halt-denial audit is
    functional in prod: a BLOCKED order returns BEFORE ``super().submit_order``,
    so NO order event ever fires on the engine msgbus — the strategy's own audit
    write is the ONLY path to record a node-side denial. ``denial_contexts`` maps
    ``str(strategy.id)`` → :class:`DenialContext` (per-run identity) so that write
    is a COMPLETE ``denied`` row via ``OrderAuditWriter.write_denied`` (idempotent
    UPSERT — durable even when no ``submitted`` row exists; PR 2 T2). The audit
    remains best-effort (a missing/raising ``_audit`` still BLOCKS — see
    :meth:`RiskAwareStrategy._record_denial`).

    The background task re-reads the fleet+account latches every ``interval_s``
    and stamps ``monotonic()`` so the synchronous gate can detect staleness.

    Arming the gate is LIVE-ONLY — backtests never reach this hook, so they stay
    inert (their ``_halt_gate_armed`` keeps the ``False`` class default).
    """
    from msai.services.nautilus.risk import RiskAwareStrategy

    gated = [s for s in strategies if isinstance(s, RiskAwareStrategy)]

    async def _read() -> bool:
        return await _read_halt_value(redis_client, account_id)

    # --- Arm the gate + inject the audit writer FIRST (fail-closed). An armed
    # gate with a None cache blocks opening orders, so a Redis blip during the
    # immediate refresh below CANNOT silently disarm the node.
    #
    # ``denial_contexts`` maps ``str(strategy.id)`` (== the member's
    # ``strategy_id_full``) → :class:`DenialContext` so a node-side halt block
    # can write a COMPLETE ``denied`` audit row (PR 2 T2). A strategy with no
    # matching context keeps ``_denial_context = None`` and falls back to the
    # UPDATE-only ``update_denied`` (best-effort; never fabricates identity).
    _contexts = denial_contexts or {}
    for strategy in gated:
        if audit_writer is not None:
            strategy._audit = audit_writer  # noqa: SLF001
        ctx = _contexts.get(str(getattr(strategy, "id", "")))
        if ctx is not None:
            strategy._denial_context = ctx  # noqa: SLF001
        strategy._halt_gate_armed = True  # noqa: SLF001

    async def _refresh_loop() -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                for strategy in gated:
                    await strategy.refresh_halt_cache(_read)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a Redis blip must not kill the loop
                # Leave the cache untouched → it ages out → the gate fails closed.
                log.warning("halt_cache_refresh_failed")

    # --- Create the background recovery task UNCONDITIONALLY (before the
    # immediate refresh) so a refresh error can't skip it.
    task = asyncio.create_task(_refresh_loop(), name=f"halt_refresh-{account_id}")

    # --- Best-effort immediate refresh: closes the cold-start window on a
    # healthy node. On error the gate stays armed with a None/stale cache (fails
    # CLOSED) and the background task above recovers when Redis returns.
    for strategy in gated:
        try:
            await strategy.refresh_halt_cache(_read)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning(
                "halt_cache_immediate_refresh_failed_gate_armed_fail_closed",
                extra={"account_id": account_id},
            )

    return task


# ---------------------------------------------------------------------------
# Payload (picklable by mp.Process under the spawn context)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyMemberPayload:
    """One strategy within a portfolio deployment.

    Carries the per-strategy fields that
    :func:`build_portfolio_trading_node_config` needs to construct an
    :class:`ImportableStrategyConfig` for each member. All fields are
    primitives / builtins (plus the frozen-dataclass ``ResolvedInstrument``
    which is itself composed of primitives — Task 11b verifies
    picklability end-to-end) so the payload remains picklable under the
    ``mp.Process`` spawn context.
    """

    strategy_id: UUID
    strategy_path: str
    strategy_config_path: str
    strategy_config: dict[str, Any] = field(default_factory=dict)
    strategy_code_hash: str = ""
    strategy_id_full: str = ""
    """Globally unique strategy identity string (``<strategy_id>@<deployment_slug>``).
    Threaded into the ``ImportableStrategyConfig.config["order_id_tag"]``
    so the audit hook can correlate orders to individual strategies
    within a portfolio deployment."""
    instruments: list[str] = field(default_factory=list)
    """Paper symbols this strategy subscribes to (e.g. ``["AAPL", "MSFT"]``).
    Aggregated across all members to build the single
    InstrumentProviderConfig for the TradingNode."""
    resolved_instruments: tuple[ResolvedInstrument, ...] = ()
    """Tuple of ``ResolvedInstrument`` from ``lookup_for_live``. Replaces
    the closed-universe ``PHASE_1_PAPER_SYMBOLS`` gate. Task 11's
    ``build_portfolio_trading_node_config`` aggregates across members,
    dedup'd by ``canonical_id``. Empty tuple default preserves existing
    test construction sites (kwarg-only field). ``ResolvedInstrument``
    is a frozen dataclass of primitives (``str``, ``StrEnum``, ``dict``,
    ``tuple[date, date | None]``) — all picklable under the spawn
    context; Task 11b verifies the round-trip."""


@dataclass(frozen=True)
class TradingNodePayload:
    """Everything a live trading subprocess needs to do its job.

    ``frozen=True`` + only-primitive fields so ``mp.Process`` can
    pickle it under the spawn context. The subprocess imports
    ``msai.*`` modules freshly inside its own interpreter — the payload
    is the only state transferred across the process boundary.
    """

    row_id: UUID
    """Primary key of the ``live_node_processes`` row the supervisor
    inserted in phase A of ``spawn``. Used by the subprocess to
    self-write pid and status transitions."""

    deployment_id: UUID
    deployment_slug: str
    strategy_path: str
    strategy_config_path: str
    strategy_config: dict[str, Any] = field(default_factory=dict)
    strategy_id: UUID | None = None
    """FK to ``strategies.id``. Needed by the engine-level audit hook
    to write valid ``order_attempt_audits`` rows."""
    strategy_code_hash: str = ""
    """SHA256 of the strategy file. Needed for audit trail."""
    paper_symbols: list[str] = field(default_factory=list)
    canonical_instruments: list[str] = field(default_factory=list)
    """Original canonical instrument IDs (e.g. ``AAPL.NASDAQ``) from the
    deployment row. Used by MarketHoursService to prime trading hours
    from the instrument_cache table (which keys on canonical_id)."""
    spawn_today_iso: str = ""
    """Exchange-local ``YYYY-MM-DD`` date computed by the supervisor at
    spawn time. Threaded through to the IB provider config builder so
    the subprocess uses the SAME front-month futures contract the
    supervisor used when canonicalizing the strategy's instrument_id.
    Without this, a spawn that crosses the midnight quarterly-roll
    boundary would preload the wrong month and the strategy would
    subscribe to a bar stream that doesn't exist. Empty string means
    "compute in the subprocess" (back-compat for older callers)."""
    ib_host: str = "127.0.0.1"
    ib_port: int = 4002
    ib_account_id: str = "DU0000000"
    database_url: str = ""
    """Async DB URL the subprocess uses to open its own
    ``AsyncEngine``. Passed explicitly (rather than reading ``settings``
    on import) so tests can point at testcontainers."""

    redis_url: str = ""
    """Redis URL the subprocess uses to construct the
    :class:`IBDisconnectHandler` (Phase 4 task 4.2). The handler
    sets ``msai:risk:halt`` when IB Gateway stays disconnected
    past its grace window so the supervisor's push-based kill
    switch tears down the deployment. Empty string means "no
    disconnect monitoring" — tests that don't care about the
    disconnect halt omit this field entirely and the subprocess
    skips the handler construction."""

    startup_health_timeout_s: float = 60.0

    strategy_members: list[StrategyMemberPayload] = field(default_factory=list)
    """Per-strategy payloads for a portfolio deployment. When non-empty,
    :func:`_build_real_node` uses :func:`build_portfolio_trading_node_config`
    instead of the single-strategy builder. The legacy single-strategy fields
    (``strategy_path``, ``strategy_config_path``, ``strategy_config``,
    ``paper_symbols``) are still populated for back-compat and supervisor
    audit, but the multi-strategy config builder reads from here."""

    # ------------------------------------------------------------------
    # Per-account broker fleet (PR 1 Task T12)
    # ------------------------------------------------------------------
    # Three pre-resolved fields populated by the supervisor's ASYNC
    # payload factory and consumed by the SYNC subprocess
    # ``_build_real_node`` path. When all three are non-empty, the
    # subprocess routes to :func:`build_per_account_trading_node_config`
    # (Databento data + IB exec topology) instead of the legacy
    # :func:`build_portfolio_trading_node_config`. The boundary lives
    # here because the supervisor has a DB session (T10's
    # ``resolve_databento_targets`` is async + DB-backed) and the
    # subprocess does not.

    ibg_client_id: int = 0
    """Pre-allocated IB Gateway client_id slot for the IB exec
    connection. Derived from ``deployment_slug`` via
    :func:`msai.services.nautilus.ibg_client_id.derive_ibg_client_id`
    (the single source of truth — Codex iter 1 P1-5). ``0`` falls
    back to the legacy hashed derivation inside the SYNC builder."""

    native_instrument_ids: list[str] = field(default_factory=list)
    """Pre-resolved Databento NATIVE instrument ids (e.g.
    ``["AAPL.XNAS"]``). Output of T10's
    :func:`resolve_databento_targets`. Empty list means "not
    populated — use the legacy builder"."""

    venue_dataset_map: dict[str, str] = field(default_factory=dict)
    """Authoritative ``native_venue → dataset`` map (e.g.
    ``{"XNAS": "EQUS.MINI"}``). Output of T10's
    :func:`resolve_databento_targets`."""

    canonical_to_native_bar_types: dict[str, str] = field(default_factory=dict)
    """Canonical ``<SYM>.IBKR-...`` → native ``<SYM>.{native_venue}-...``
    bar-type strings. Built by the supervisor by zipping each
    strategy's canonical bar_type with the native venue from the
    shim's resolution. Threaded into the SymbologyShimActor config
    so its ``on_start`` knows which native bar types to subscribe
    to. Empty dict means "not populated — use the legacy builder"."""

    # ------------------------------------------------------------------
    # PR 1b — data-stale auto-halt (Task T4)
    # ------------------------------------------------------------------
    data_freshness_enabled: bool = True
    """Master switch for the in-node Databento freshness monitor + the
    reconciled-marker writes. When True (the default), the live run loop
    builds a :class:`~msai.services.nautilus.data_stale_monitor.DataStaleMonitor`
    (even on a legacy node with no Databento feeds — it then publishes an
    EMPTY manifest) and DELETEs/SETs the reconciled marker. When False the
    whole freshness/marker path is a no-op (existing flows unaffected).
    Primitive bool so the payload stays picklable under the spawn context.

    TEST-ONLY escape hatch — NOT a supported runtime opt-out (Codex iter-11
    open question). There is no production override path that ever sets this
    False: the default is True and nothing in the deploy/start flow flips it.
    The field exists so unit tests can construct a payload that skips the
    monitor wiring. A PRODUCTION deployment ALWAYS runs the monitor — a
    legacy / no-feed node is covered by the EMPTY-manifest case (which
    data-health renders as vacuous-warm and never pages ``monitor_missing``),
    so there is no legitimate "running but no monitor" configuration. If a
    deployment WERE somehow started with this False, it would publish NO
    manifest → data-health WILL page ``monitor_missing`` and ``/resume`` WILL
    fail closed (absent verdict = blocking). That is the intended fail-closed
    posture for an unsupported configuration, not a bug."""

    data_freshness_grace_json: str | None = None
    """Optional JSON override for :class:`GraceConfig`, flowing into
    ``GraceConfig.from_env_json`` at monitor-wiring time. ``None`` → defaults.
    Primitive string (not a model) so the payload stays picklable."""

    @property
    def use_per_account_topology(self) -> bool:
        """Whether the subprocess should route to the per-account
        Databento + IB-exec topology (PR 1 T11's
        :func:`build_per_account_trading_node_config`).

        True iff the supervisor populated ALL three pre-resolved
        fields. We require the bar-type map to be non-empty because
        without it the ``SymbologyShimActor.on_start`` has nothing to
        subscribe to and bars never reach the strategy (Codex iter
        7 P1 of PR 1). An empty bar-type map is a programming bug
        upstream — fail-fast by NOT activating the new path.
        """
        return bool(
            self.native_instrument_ids
            and self.venue_dataset_map
            and self.canonical_to_native_bar_types
        )

    @property
    def all_instruments(self) -> list[str]:
        """De-duplicated, sorted union of instruments across all strategy members.

        Returns an empty list when ``strategy_members`` is empty (legacy
        single-strategy path — the caller uses ``paper_symbols`` instead).
        """
        if not self.strategy_members:
            return []
        seen: set[str] = set()
        for member in self.strategy_members:
            seen.update(member.instruments)
        return sorted(seen)


# ---------------------------------------------------------------------------
# Protocol types
# ---------------------------------------------------------------------------


# Type alias for the factory callable. Kept untyped (``Any``) to avoid
# importing ``nautilus_trader`` at module load time — the subprocess's
# imports are expensive and every test that touches this module would
# pay the cost.
NodeFactory = "Callable[[TradingNodePayload], Any]"


# ---------------------------------------------------------------------------
# DB write helpers (async — run inside asyncio.run loop)
# ---------------------------------------------------------------------------


async def _update_row(
    session_factory: async_sessionmaker[AsyncSession],
    row_id: UUID,
    **values: Any,
) -> None:
    """Atomic UPDATE of a single ``live_node_processes`` row."""
    async with session_factory() as session, session.begin():
        await session.execute(
            update(LiveNodeProcess).where(LiveNodeProcess.id == row_id).values(**values)
        )


async def _self_write_pid(
    session_factory: async_sessionmaker[AsyncSession],
    row_id: UUID,
) -> None:
    """Write the subprocess's own pid onto its ``live_node_processes`` row.

    Runs BEFORE any Nautilus import so ``pid`` is populated even if
    the build path throws. Also transitions ``status`` from
    ``'starting'`` (set by the supervisor) to ``'building'`` and
    bumps the heartbeat so the heartbeat monitor doesn't immediately
    flag the row stale.
    """
    now = datetime.now(UTC)
    await _update_row(
        session_factory,
        row_id,
        pid=os.getpid(),
        status="building",
        last_heartbeat_at=now,
    )


async def _mark_ready(session_factory: async_sessionmaker[AsyncSession], row_id: UUID) -> None:
    await _update_row(
        session_factory,
        row_id,
        status="ready",
        last_heartbeat_at=datetime.now(UTC),
    )


async def _mark_running(session_factory: async_sessionmaker[AsyncSession], row_id: UUID) -> bool:
    """Write the node's ``running`` row state AND forward-sync the parent
    ``live_deployments.status`` to ``running``.

    Returns ``True`` iff the node row was actually PROMOTED to ``running``
    (a real self-promotion happened). Returns ``False`` when promotion was
    SKIPPED — the node/deployment row could not be resolved, OR a concurrent
    ``/stop`` / ``/kill-all`` already stamped stop intent on the node row. The
    caller uses this to decide whether to SET the reconciled marker (Codex
    iter-20 P1): the marker must be SET only on a real promotion, never on a
    stop-raced startup where the row was deliberately left unpromoted — else
    ``/resume`` (which trusts the reconciled marker for ACTIVE deployments,
    including ``stopping``) could clear a halt against a node that is going
    down.

    PR 2 T4 review P1: the ``/start-portfolio`` API poll only waits 60s, but a
    slow IB connect/reconcile can push the node's ``running`` transition past
    that window. When that happens the API leaves the deployment in its
    non-terminal ``starting`` / ``building`` status (it no longer flips it to
    ``failed`` — that would orphan a real-money node). But absent this
    forward-sync the node ONLY ever wrote terminal statuses to the deployment
    row, so the deployment would linger at ``starting`` forever even though a
    live node is trading. Forward-syncing here means the logical deployment
    view eventually reflects ``running`` with no operator retry required.

    The non-terminal guard (``starting`` / ``building`` / ``ready``) makes this
    a no-op once a concurrent ``/live/stop`` / kill-all has already moved the
    deployment to ``stopping`` / ``stopped`` / ``failed`` — we never resurrect
    a deployment the operator is tearing down.

    **Lost-update lock (prior-review P2).** The guard is only sound if the
    read-check-write of ``deployment.status`` is serialized against a
    concurrent ``/stop`` / ``/kill-all`` transaction. Without a row lock,
    under READ COMMITTED this transaction could read ``building`` on an
    unlocked snapshot, then have its UPDATE block on the row lock the
    concurrent stop's UPDATE holds, and finally OVERWRITE ``stopped`` /
    ``stopping`` back to ``running`` after the stop commits. We therefore
    fetch the ``LiveDeployment`` row ``FOR UPDATE`` (``with_for_update``) so
    the conditional is evaluated against the value seen UNDER the lock: if a
    concurrent stop committed first, our ``FOR UPDATE`` fetch blocks until it
    releases, then re-reads the terminal/``stopping`` status and the guard
    skips the write. The node-process row is locked the same way so the two
    writes stay in one consistent critical section.

    **Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST).** This is
    the SOLE subprocess writer that locks BOTH rows. The lock order is
    ``LiveDeployment FOR UPDATE`` FIRST, THEN ``LiveNodeProcess FOR UPDATE`` —
    matching the global supervisor invariant ``advisory(gateway) →
    live_deployments FOR UPDATE → live_node_processes FOR UPDATE``. The prior
    order locked the node row FIRST then the deployment (a node→deployment edge),
    which could cycle with the operator-/stop + give-up + Phase-A paths (all
    deployment-first) and deadlock the real-money stop path. Re-ordering does NOT
    weaken the lost-update guard: the deployment row is still evaluated UNDER its
    lock; we simply acquire it before the node row.
    """
    from msai.models.live_deployment import LiveDeployment

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        # Deployment FOR UPDATE FIRST (council 2026-06-01 lock-order invariant),
        # then the owned node row. We need the deployment_id to lock the parent,
        # which lives on the node row — so resolve it with a plain (unlocked)
        # read of just that column, then take the deployment lock, then the node
        # lock. The plain id read acquires NO row lock, so it adds no edge.
        deployment_id = (
            await session.execute(
                select(LiveNodeProcess.deployment_id).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one_or_none()
        if deployment_id is None:
            return False
        # FOR UPDATE so the status guard below races correctly with a concurrent
        # /stop or /kill-all UPDATE on the same deployment row — acquired FIRST.
        deployment = await session.get(LiveDeployment, deployment_id, with_for_update=True)

        process_row = await session.get(LiveNodeProcess, row_id, with_for_update=True)
        if process_row is None:
            return False

        # PR 2 / Codex iter-28 P1 — preserve operator-stop intent across the
        # startup→running self-promotion. ``_mark_running`` is the CHILD's
        # self-write of "running" once it reaches ``is_running`` (legitimately
        # called while the row is in any pre-running active state:
        # ``starting`` → ``building`` → ``ready`` → ``running``). If an operator
        # ``/stop`` (or ``/kill-all``) raced with startup and already moved this
        # row into a stop/terminal state — stamping ``stop_requested_at`` and/or
        # flipping ``status`` to ``stopping``/``stopped``/``failed`` — promoting
        # it back to "running" here would ERASE that durable stop intent and, in
        # the no-pid / supervisor-restart windows, leave a STOPPED account
        # looking active until a later terminal write. The ``FOR UPDATE``
        # acquired above serializes against the ``/stop`` writer's own row lock,
        # so a stop that committed first is visible here. Leave the row exactly
        # as the stop path left it (that path + the reaper terminalize it and
        # suppress auto-restart) and SKIP the deployment promotion below too, so
        # ``/live/status`` keeps showing the operator's stop rather than a
        # resurrection. ``record_success`` is also skipped — a node being torn
        # down did not "restart healthily". We enumerate the stop/terminal
        # statuses (NOT a promotable allow-list) so a new pre-running state can
        # never be silently excluded from a healthy promotion.
        if process_row.stop_requested_at is not None or process_row.status in (
            "stopping",
            "stopped",
            "failed",
        ):
            log.info(
                "mark_running_skipped_stop_intent_present",
                extra={
                    "row_id": str(row_id),
                    "deployment_id": str(deployment_id),
                    "status": process_row.status,
                    "stop_requested": process_row.stop_requested_at is not None,
                },
            )
            return False

        process_row.status = "running"
        process_row.last_heartbeat_at = now

        # PR 2 T6 — healthy-reconcile RESET of the auto-restart authority.
        #
        # ``wait_until_ready`` (gotcha #10: reconciliation verified via
        # ``kernel.trader.is_running``) has just succeeded, so this node
        # reached ``is_running`` AND reconciled. That is exactly the
        # "successful restart" signal :meth:`RestartPolicy.record_success`
        # is defined for: clear the consecutive-failure streak + the pause
        # latch so a node that crashes, restarts, runs healthily, then later
        # suffers an INDEPENDENT transient crash starts a FRESH rolling
        # window rather than accumulating across every successful restart
        # toward the ceiling (prior-review P1: ``record_success`` was dead
        # code — the only reset path was the 30-min window in
        # ``record_failure``; this is the production call site that wires
        # the documented "healthy reconcile resets the streak" contract).
        #
        # Call the policy's mutator (single source of the reset semantics)
        # rather than duplicating its field writes inline. The counters live
        # on the per-spawn row but are CARRIED FORWARD across respawn
        # generations by Phase A (see ``FleetRouter._phase_a_reserve_slot``
        # ``restart_carry``), so resetting them here on the live row is the
        # authoritative "streak ended healthily" mutation.
        from msai.live_supervisor.restart_policy import RestartPolicy

        RestartPolicy().record_success(process_row)

        # The deployment row was already locked FOR UPDATE at the TOP of this
        # transaction (council 2026-06-01 deployment-first invariant). Evaluate
        # the lost-update guard against that locked value: a concurrent /stop /
        # /kill-all that committed first is already visible here, so we never
        # resurrect a deployment the operator is tearing down.
        if deployment is not None and deployment.status in ("starting", "building", "ready"):
            deployment.status = "running"
            deployment.last_stopped_at = None

    # A real self-promotion happened (the node row was set ``running``). The
    # deployment forward-sync above is best-effort under its own guard, but the
    # node-row promotion is the authoritative "this node reached running + was
    # not racing a stop" signal that gates the reconciled-marker SET.
    return True


async def _mark_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    row_id: UUID,
    *,
    status: str,
    failure_kind: FailureKind,
    error_message: str | None,
    exit_code: int,
) -> None:
    """Write the subprocess's terminal row state AND sync the parent
    ``live_deployments.status`` so the logical deployment view doesn't
    linger at ``running`` / ``starting`` after the process has exited.

    Without the deployment-row sync, ``/api/v1/live/status`` shows
    ``deployment.status='running'`` indefinitely after ``/live/stop``
    or ``/kill-all`` — the process row flips to ``stopped``/``failed``
    but the logical row is untouched. This extends the Bug X3 fix
    (which only covered the spawn-failure path in ``_mark_failed``)
    to also cover the normal-stop path that terminates through this
    function.

    **Council 2026-06-01 — Finding 1 (P0 DEADLOCK FIX): deployment-FIRST.**
    The prior implementation used a plain ``session.get`` on the node row
    THEN the deployment row, with NO row locks. That was WRONGLY exempted
    from the lock-order invariant: a dirty-flush ORM UPDATE still acquires a
    row-level WRITE lock held until commit, and SQLAlchemy's unit-of-work
    flushes UPDATEs in the order objects became dirty (it does NOT reorder by
    FK for UPDATEs) — so writing the node row before the deployment row was a
    ``live_node_processes → live_deployments`` lock-acquisition edge. Run
    concurrently with the operator-``/stop`` deployment-first path that edge
    closes a D→N→D cycle and DEADLOCKS the real-money stop path (a reviewer
    reproduced it 15/15 rounds on Postgres). We now acquire the
    ``LiveDeployment`` row lock ``FOR UPDATE`` FIRST, then the ``LiveNodeProcess``
    row ``FOR UPDATE`` — matching the global invariant ``advisory(gateway) →
    live_deployments FOR UPDATE → live_node_processes FOR UPDATE`` and mirroring
    the already-correct :func:`_mark_running`. The deployment_id lives on the
    node row, so we resolve it with a PLAIN (unlocked) column read first (that
    read acquires no row lock, so it adds no edge), then take the two locks
    deployment-first.

    **"Terminal write LAST" is preserved.** That contract (see the caller's
    shutdown sequence: dispose → heartbeat-stop → terminal DB write) is about
    WRITE ORDERING — which row's status commits last, and at which point in the
    subprocess teardown the row leaves the active set — NOT about
    lock-acquisition order WITHIN this transaction. We still write the node
    status (the active-set-releasing write) here at the end of the subprocess's
    teardown, and the whole transaction commits atomically; acquiring the
    deployment lock first inside this single transaction does not change WHEN the
    node status becomes visible. The slot-release ordering therefore holds.
    """
    from msai.models.live_deployment import LiveDeployment

    async with session_factory() as session, session.begin():
        # Deployment FOR UPDATE FIRST (council 2026-06-01 deployment-first
        # invariant), then the owned node row. The deployment_id lives on the
        # node row, so resolve it with a PLAIN (unlocked) column read — that read
        # acquires NO row lock, so it adds no edge — then take the deployment lock
        # before the node lock.
        deployment_id = (
            await session.execute(
                select(LiveNodeProcess.deployment_id).where(LiveNodeProcess.id == row_id)
            )
        ).scalar_one_or_none()
        # FOR UPDATE FIRST so the non-terminal status guard below races correctly
        # with a concurrent ``/live/stop`` / ``/kill-all`` UPDATE on the same
        # deployment row, and so the global lock order stays deployment→node.
        deployment = (
            await session.get(LiveDeployment, deployment_id, with_for_update=True)
            if deployment_id is not None
            else None
        )

        process_row = await session.get(LiveNodeProcess, row_id, with_for_update=True)
        if process_row is None:
            return

        # LiveNodeProcess terminal write. This is the active-set-releasing write
        # ("Terminal write LAST" — see docstring); the node lock is held under the
        # deployment lock taken above, so the lock-acquisition order is
        # deployment→node even though we WRITE the node row here.
        process_row.status = status
        process_row.failure_kind = failure_kind.value
        process_row.error_message = error_message
        process_row.exit_code = exit_code
        process_row.last_heartbeat_at = datetime.now(UTC)

        # Map the subprocess's terminal status onto the deployment row.
        # "stopped" → "stopped" (clean shutdown, can be restarted).
        # Everything else (failed, stale, unmanaged, ...) → "failed".
        # The non-terminal guard lets a concurrent ``/live/stop`` that
        # already set ``stopped`` take precedence over a subsequent
        # ``failed``-path terminal write (e.g., if SIGTERM arrived but
        # the subprocess then crashed during teardown). Evaluated UNDER the
        # deployment lock taken at the top, so a concurrent stop that committed
        # first is already visible here.
        if deployment is not None and deployment.status in (
            "starting",
            "building",
            "ready",
            "running",
        ):
            deployment.status = "stopped" if status == "stopped" else "failed"


# ---------------------------------------------------------------------------
# Heartbeat thread (Phase 1 task 1.9)
# ---------------------------------------------------------------------------


class _HeartbeatThread(threading.Thread):
    """Background thread that bumps ``live_node_processes.last_heartbeat_at``.

    Why a thread instead of an asyncio task in the main loop: Nautilus's
    event loop owns the async context once ``node.run()`` takes over,
    so we can't schedule coroutines on it from the outside. A daemon
    thread that runs its OWN asyncio loop (with its own asyncpg-backed
    engine) is the simplest way to keep the heartbeat writing through
    ``build`` → ``start_async`` → ``run``. The heartbeat doesn't need
    low latency (the stale threshold is 30s); async is just a
    convenience so we don't need a second sync DB driver.

    **Ordering** (decision #17, enforced in task 1.8): the heartbeat
    starts BEFORE ``node.build()``, immediately after the subprocess
    self-writes ``pid`` and ``status='building'``. It runs continuously
    through build, ``start_async``, ``wait_until_ready``, and
    ``node.run()``. It is stopped in the ``finally`` block BEFORE
    ``node.stop_async()`` + ``node.dispose()`` so the heartbeat thread
    can't outlive the row it's writing to.
    """

    def __init__(
        self,
        *,
        async_database_url: str,
        row_id: UUID,
        interval_s: float = 5.0,
    ) -> None:
        super().__init__(daemon=True, name=f"heartbeat-{row_id.hex[:8]}")
        self._url = async_database_url
        self._row_id = row_id
        self._interval_s = interval_s
        self._stop_event = threading.Event()
        self._ticks = 0
        self._last_error: Exception | None = None

    def stop(self) -> None:
        """Signal the thread to exit on the next wake-up."""
        self._stop_event.set()

    @property
    def ticks(self) -> int:
        """Number of successful heartbeat writes since start. Used by
        tests to assert the thread actually ran during build."""
        return self._ticks

    @property
    def last_error(self) -> Exception | None:
        return self._last_error

    def run(self) -> None:  # noqa: D401 — Thread.run override
        """Spin up a private asyncio loop and run the async heartbeat
        loop inside it. ``asyncio.run`` creates + tears down the loop
        cleanly when the loop coroutine returns (on stop())."""
        try:
            asyncio.run(self._async_loop())
        except Exception as exc:  # noqa: BLE001
            # asyncio.run itself could raise on interpreter shutdown —
            # catch it so the thread exits cleanly.
            log.exception("heartbeat_thread_loop_failed")
            self._last_error = exc

    async def _async_loop(self) -> None:
        """Main heartbeat loop — runs in the thread's private loop.

        Uses its OWN ``AsyncEngine`` + ``async_sessionmaker`` so it
        doesn't share connections with the subprocess's main loop
        (which lives in a different thread and therefore owns a
        different event loop).
        """
        engine = create_async_engine(self._url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            while not self._stop_event.is_set():
                try:
                    async with factory() as session, session.begin():
                        await session.execute(
                            update(LiveNodeProcess)
                            .where(LiveNodeProcess.id == self._row_id)
                            .values(last_heartbeat_at=datetime.now(UTC))
                        )
                    self._ticks += 1
                except Exception as exc:  # noqa: BLE001
                    # Never let a transient DB blip kill the loop —
                    # the supervisor's HeartbeatMonitor stale sweep is
                    # the backstop. Log + keep going.
                    log.exception("heartbeat_tick_failed")
                    self._last_error = exc

                # Interruptible sleep — poll ``_stop_event`` so
                # ``stop()`` returns within ``poll_step`` seconds
                # rather than waiting out the full interval.
                poll_step = min(0.1, self._interval_s)
                elapsed = 0.0
                while elapsed < self._interval_s and not self._stop_event.is_set():
                    await asyncio.sleep(poll_step)
                    elapsed += poll_step
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Subprocess async core — tests call this directly
# ---------------------------------------------------------------------------


async def run_subprocess_async(
    payload: TradingNodePayload,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    node_factory: Any,
    heartbeat_factory: Any = None,
    disconnect_handler_factory: Any = None,
    install_signal_handlers: bool = False,
    shutdown_event: asyncio.Event | None = None,
    skip_dispose: bool = False,
    on_node_constructed: Any = None,
    on_post_build: Any = None,
    async_cleanup: Any = None,
    data_freshness_monitor_factory: Any = None,
    reconciled_marker_factory: Any = None,
) -> int:
    """Execute one trading subprocess lifecycle end-to-end.

    Returns the exit code the caller should exit with:

    - ``0`` — clean stop
    - ``1`` — generic exception (build/start/run failure)
    - ``2`` — startup health check timed out

    All terminal writes go through :func:`_mark_terminal` so the
    ``failure_kind`` column is always populated for the
    ``/api/v1/live/start`` endpoint to read.

    Order of operations (decision #17 / Codex v5 P0, refactored
    in iter10 P0 to use the real ``TradingNode.run_async`` API):

    1. Self-write pid + ``status='building'``
    2. Start heartbeat thread (BEFORE node.build — decision #17)
    3. ``node = node_factory(payload)``
    4. ``node.build()``
    5. ``node_run_task = asyncio.create_task(node.run_async())`` —
       Nautilus's ``run_async`` first does ``kernel.start_async``
       (which flips ``trader.is_running`` to True) then blocks on
       ``asyncio.gather`` over the engine queue tasks until
       ``stop_async`` is called or an engine task fails.
    6. ``await wait_until_ready(node)`` — polls
       ``trader.is_running`` concurrently with ``run_async``
    7. If ``run_async`` already crashed during start, surface the
       exception from the task instead of marking ready
    8. ``status='ready'``, ``status='running'``
    9. ``await node_run_task`` — blocks until SIGTERM (which
       schedules ``stop_async``) or internal engine failure
    10. finally: ``node.stop_async()`` (idempotent if already
        stopped) → ``node.dispose()`` → ``heartbeat.stop()`` →
        terminal status write

    Args:
        payload: Everything the subprocess needs to know about the
            deployment.
        session_factory: Async session factory the DB writes go through.
        node_factory: Callable that takes ``payload`` and returns a
            ``TradingNode``-like object (the real factory in production,
            a stub in tests).
        heartbeat_factory: Optional callable that takes ``payload`` and
            returns an object with ``start()`` / ``stop()`` methods
            (typically a :class:`_HeartbeatThread`). Tests that don't
            care about heartbeats pass ``None`` to skip it entirely.
            Production callers always pass a real factory.
        disconnect_handler_factory: Optional async callable that takes
            ``(payload, node)`` and returns an awaitable
            :class:`IBDisconnectHandler` instance (Phase 4 task 4.2
            wiring). When present, the handler runs as a sibling task
            to ``node_run_task`` and fires the Redis halt flag if IB
            Gateway stays disconnected past the grace window. Tests
            pass ``None`` to skip; production passes a real factory
            that opens an aioredis client and wires the connection
            probe against the node's data engine.
        install_signal_handlers: When True, register async-aware
            SIGTERM/SIGINT handlers on the running loop that set the
            ``shutdown_event`` and schedule ``node.stop_async()``.
            Production callers pass True; tests typically pass False
            and drive shutdown via ``shutdown_event`` directly.
        shutdown_event: Optional externally-owned ``asyncio.Event``
            that signals "please abort startup / tear down". If omitted,
            a fresh private event is created. Tests inject their own
            event to deterministically drive the "SIGTERM mid-startup"
            code path without relying on signal timing — see
            ``test_trading_node_subprocess.py`` (Codex batch 3 iter2
            P1 regression tests).
        skip_dispose: When True, the finally block does NOT call
            ``node.dispose()``. Required for production callers
            because Nautilus 1.223.0 ``TradingNode.dispose()`` calls
            ``loop.stop()`` if the kernel's loop is currently
            running, which is exactly the loop ``asyncio.run`` is
            blocked on — that breaks ``asyncio.run`` with
            ``RuntimeError: Event loop stopped before Future
            completed`` (Codex batch 3 iter11 P0 fix). The
            production wrapper passes ``True`` and disposes the
            node AFTER ``asyncio.run`` returns. Tests use the
            default ``False`` because their fake ``dispose()`` is
            a no-op and the test loop is unaffected.
        on_node_constructed: Optional callback invoked the moment
            ``node = node_factory(payload)`` returns. Production
            uses it to capture the node for the post-loop dispose
            step (paired with ``skip_dispose=True``).
        data_freshness_monitor_factory: Optional async callable
            ``(payload, node) -> monitor | None`` (PR 1b T4). When
            present AND ``payload.data_freshness_enabled`` is True, the
            run loop calls it AFTER the node reaches ``running`` (same
            point as ``disconnect_handler_factory``); the production
            factory builds the shared ``FreshnessRegistry``, retrieves
            the ``DataFreshnessActor`` from ``node.trader.actors()`` and
            injects the registry via ``set_registry``, derives
            ``required_feeds`` from the actor's dataset map, and returns
            a :class:`DataStaleMonitor`. The run loop then ``await``s the
            monitor's ``start()`` and ``stop()``s it in the finally block.
            A legacy node (empty bar-type map) STILL runs the monitor with
            an empty required-feed universe (it publishes the EMPTY
            manifest). Tests pass a stub; ``None`` skips the path.
        reconciled_marker_factory: Optional async callable
            ``(payload) -> marker | None`` (PR 1b T4). When present AND
            ``payload.data_freshness_enabled`` is True, the run loop calls
            it BEFORE node start and ``await``s ``marker.clear()`` (DELETE
            the reconciled marker so a restart re-arms fail-closed), then
            ``await``s ``marker.mark_reconciled()`` (SET it) ONLY after
            ``_mark_running`` succeeds — i.e. after ``wait_until_ready``
            proved ``trader.is_running`` (which in nautilus 1.223.0 cannot
            flip True without a healthy reconcile; see
            :func:`~msai.core.halt_keys.reconciled_key`). If readiness
            fails the marker stays absent (fail-closed). Tests pass a stub;
            ``None`` skips the path.
    """
    # Note: ``_self_write_pid`` and the heartbeat-thread start are
    # NOT run here — they live inside the main ``try`` block below
    # (Codex batch 3 iter7 P3 fix). If either of those raises before
    # the guard, the function would exit without ever running the
    # ``finally`` block's terminal write, and the operator would see
    # the reap loop's generic ``child exited with code 1`` instead
    # of the actual traceback. Inside the guard, the catch-all
    # ``except`` records the failure into the terminal-state
    # locals and the ``finally`` persists them.
    heartbeat: Any = None
    node: Any = None
    # Phase 4 task 4.2 iter-2 wiring: optional sibling task that
    # watches the node's data-engine connection state and fires
    # the Redis halt flag if IB stays disconnected past the
    # grace window. Nullable so tests that don't care about
    # disconnect monitoring can pass ``disconnect_handler_factory=None``
    # and the whole path is a no-op.
    disconnect_handler: Any = None
    disconnect_task: asyncio.Task[None] | None = None

    # PR 1b T4: in-node Databento data-stale monitor + reconciled marker.
    # Both nullable so a caller that omits the factories (or disables
    # freshness via the payload) gets a complete no-op path.
    data_stale_monitor: Any = None
    reconciled_marker: Any = None
    freshness_enabled = bool(payload.data_freshness_enabled)

    # Async-loop-aware SIGTERM handler. Runs in the context of the
    # running event loop (thanks to ``loop.add_signal_handler``), so
    # it can safely schedule ``node.stop_async()`` as a task. A raw
    # ``signal.signal`` handler can't drive async shutdown from a
    # foreign context (Codex batch 3 P1 fix).
    shutdown_requested = shutdown_event if shutdown_event is not None else asyncio.Event()
    loop: asyncio.AbstractEventLoop | None = None
    if install_signal_handlers:
        loop = asyncio.get_running_loop()

        def _on_sigterm() -> None:
            log.info(
                "trading_node_sigterm_received",
                extra={"row_id": str(payload.row_id)},
            )
            shutdown_requested.set()
            if node is not None:
                # Schedule the stop on the loop — this will return
                # node.run() once Nautilus finishes its stop handshake.
                # stop_async is idempotent so double-invocation (e.g.
                # two SIGTERMs) is fine.
                asyncio.create_task(node.stop_async())

        try:
            loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
            loop.add_signal_handler(signal.SIGINT, _on_sigterm)
        except (NotImplementedError, RuntimeError):
            # Some platforms (Windows) don't support add_signal_handler.
            # Fall back to the default terminate-on-SIGTERM behavior;
            # the subprocess will die uncleanly and the supervisor's
            # reap loop will catch up via the child's exit code.
            log.warning(
                "trading_node_signal_handler_unavailable",
                extra={"row_id": str(payload.row_id)},
            )

    # Terminal outcome — recorded on every code path and persisted
    # ONLY in the finally block, AFTER cleanup has finished. Writing
    # terminal status before ``heartbeat.stop()``, ``node.stop_async()``,
    # and ``dispose()`` would drop the row out of the active-status
    # set while this subprocess is still alive holding the IB sockets
    # and the Rust-side logger — a fast stop/restart could then
    # reserve a new row and spawn a second child for the same
    # deployment before this one has finished releasing its resources
    # (Codex batch 3 iter3 P1 fix).
    terminal_status: str = "failed"
    terminal_failure_kind: FailureKind = FailureKind.SPAWN_FAILED_PERMANENT
    terminal_error: str | None = None
    terminal_exit_code: int = 1

    def _record_clean_exit(where: str) -> None:
        """Mark this invocation as a clean ``stopped`` termination.

        Used by the startup-shutdown checkpoints so a SIGTERM arriving
        mid-startup becomes a deterministic clean shutdown. The actual
        DB write is deferred to the ``finally`` block.
        """
        nonlocal terminal_status, terminal_failure_kind, terminal_error, terminal_exit_code
        log.info(
            "trading_node_shutdown_during_startup",
            extra={
                "row_id": str(payload.row_id),
                "deployment_id": str(payload.deployment_id),
                "where": where,
            },
        )
        terminal_status = "stopped"
        terminal_failure_kind = FailureKind.NONE
        terminal_error = None
        terminal_exit_code = 0

    try:
        # Self-write the pid + flip status to ``building`` (decision
        # #17 / Codex v5 P0). Inside the guard so a DB blip here
        # still produces a structured terminal write via the
        # except/finally below (Codex batch 3 iter7 P3 fix).
        await _self_write_pid(session_factory, payload.row_id)

        # Start the heartbeat thread BEFORE node.build() so a hung
        # build continues to advance last_heartbeat_at and ages out
        # via the supervisor's watchdog stale threshold (decision
        # #17). Inside the guard for the same iter7 P3 reason: a
        # thread-start failure here is recorded as a structured
        # ``SPAWN_FAILED_PERMANENT`` instead of being lost.
        if heartbeat_factory is not None:
            heartbeat = heartbeat_factory(payload)
            heartbeat.start()

        # PR 1b T4: build the reconciled marker + DELETE it BEFORE the node
        # is built/started. A restart of the same deployment must RE-ARM the
        # fail-closed default — the marker can never be carried over from a
        # prior incarnation. The marker is SET only after ``_mark_running``
        # below (i.e. after a healthy reconcile). Gated on
        # ``data_freshness_enabled`` so a disabled deployment is a full no-op.
        if freshness_enabled and reconciled_marker_factory is not None:
            reconciled_marker = await _maybe_await(reconciled_marker_factory(payload))
            if reconciled_marker is not None:
                await reconciled_marker.clear()

        # Earliest shutdown checkpoint (Codex batch 3 iter6 P2 fix).
        # If SIGTERM lands between ``loop.add_signal_handler`` and
        # the first ``await``, the handler has already set
        # ``shutdown_requested`` but our checkpoints downstream
        # wouldn't observe it until AFTER ``node_factory`` and a
        # potentially-multi-second ``node.build()``. The
        # ``await asyncio.sleep(0)`` yields once so the loop can
        # dispatch any pending signal callback queued during the
        # handler-install window; the subsequent ``is_set()`` check
        # then catches an early operator stop and skips Nautilus
        # construction entirely.
        if install_signal_handlers:
            await asyncio.sleep(0)
        if shutdown_requested.is_set():
            _record_clean_exit("before_node_factory")
            return terminal_exit_code

        node = node_factory(payload)
        if on_node_constructed is not None:
            on_node_constructed(node)
        # node.build() is called directly on the loop thread — NOT
        # via ``asyncio.to_thread``. Nautilus's IB adapter factories
        # instantiate ``asyncio.Queue`` / ``asyncio.Event`` and call
        # ``self._create_task`` (a Cython-bound loop.create_task) from
        # inside ``InteractiveBrokersClient.__init__`` + ``_start``.
        # Those APIs bind to the running loop in the CALLING thread —
        # running them from a worker thread either binds the queues
        # to the wrong loop or raises ``RuntimeError: no running
        # event loop``. Nautilus's own examples all call ``build()``
        # synchronously from the main thread for this reason.
        #
        # Consequence: while ``build()`` is blocked on IB contract
        # loading, the loop can't dispatch signal-handler callbacks.
        # SIGTERM lands but is processed only AFTER ``build()``
        # returns. That is acceptable because:
        #   1. The heartbeat thread keeps writing
        #      (gotcha #20 / decision #17) so the watchdog sees
        #      progress
        #   2. The supervisor's ``startup_hard_timeout_s`` (default
        #      1800 s) watchdog kills stuck ``building`` rows via
        #      SIGKILL, which Python can't mask
        #   3. The post-build ``shutdown_requested.is_set()`` check
        #      catches any SIGTERM that landed during build
        # NO ``asyncio.wait_for`` around it — the supervisor
        # watchdog is the external kill switch for wedged builds.
        node.build()

        # Post-build hook: lets the production wrapper inject
        # collaborators (e.g., MarketHoursService check) into the
        # strategy after Nautilus has constructed it during build().
        if on_post_build is not None:
            await on_post_build(node, payload, session_factory)

        if shutdown_requested.is_set():
            _record_clean_exit("after_build")
            return terminal_exit_code

        # Nautilus's ``TradingNode.run_async()`` (verified against
        # ``nautilus_trader 1.223.0`` at ``live/node.py:338-377``) is
        # the SOLE async entry point: it does
        # ``await self.kernel.start_async()`` first (which is what
        # flips ``trader.is_running`` to True per decision #14) and
        # then ``asyncio.gather`` over the engine queue tasks, which
        # block forever until cancelled or stopped via
        # ``stop_async()``. There is no separate ``start_async()``
        # method on TradingNode (Codex batch 3 iter10 P0 fix);
        # earlier iterations of this module called a fictional
        # ``start_async()`` and would have crashed the moment the
        # production node factory replaced its stub.
        #
        # We schedule ``run_async()`` as a task on the SAME loop
        # this coroutine is running on (TradingNode binds to the
        # current loop at construction). Then we poll
        # ``wait_until_ready`` concurrently. The task continues
        # running until ``stop_async`` flips the kernel down, at
        # which point ``run_async`` falls out of its
        # ``asyncio.gather`` and returns — the cleanup path in
        # ``finally`` awaits the task to make sure shutdown is
        # observed.
        node_run_task: asyncio.Task[None] = asyncio.create_task(
            node.run_async(),
            name=f"trading_node_run_async-{payload.deployment_slug}",
        )

        try:
            await wait_until_ready(
                node,
                timeout_s=payload.startup_health_timeout_s,
                shutdown_event=shutdown_requested,
            )
        except StartupHealthCheckFailed as exc:
            diagnosis = str(exc)
            log.error(
                "startup_health_check_failed",
                extra={
                    "row_id": str(payload.row_id),
                    "deployment_id": str(payload.deployment_id),
                    "diagnosis": diagnosis,
                },
            )
            # Cancel the still-running ``run_async`` task — finally
            # will await it as part of cleanup.
            node_run_task.cancel()
            terminal_status = "failed"
            terminal_failure_kind = FailureKind.RECONCILIATION_FAILED
            terminal_error = diagnosis
            terminal_exit_code = 2
            return terminal_exit_code

        # ``wait_until_ready`` may also exit because ``run_async``
        # crashed during ``kernel.start_async`` and the task is
        # already done with an exception. Surface that as a
        # spawn-failure rather than misclassifying as ready.
        if node_run_task.done():
            exc_from_task = node_run_task.exception()
            if exc_from_task is not None:
                log.exception(
                    "trading_node_run_async_failed_during_startup",
                    exc_info=exc_from_task,
                )
                terminal_status = "failed"
                terminal_failure_kind = FailureKind.SPAWN_FAILED_PERMANENT
                terminal_error = "".join(
                    traceback.format_exception(
                        type(exc_from_task), exc_from_task, exc_from_task.__traceback__
                    )
                )
                terminal_exit_code = 1
                return terminal_exit_code

        if shutdown_requested.is_set():
            node_run_task.cancel()
            _record_clean_exit("after_wait_until_ready")
            return terminal_exit_code

        await _mark_ready(session_factory, payload.row_id)

        # PR 1b T4 (FIX 2 — reordered before ``_mark_running``): build + START
        # the in-node data-stale monitor BEFORE the deployment row flips to
        # ``running``, so a ``running`` row STRUCTURALLY implies the freshness
        # manifest already exists. Previously the monitor started AFTER
        # ``_mark_running``, leaving a sub-second window where a data-health
        # scrape could observe a ``running`` row with no manifest yet and
        # report a spurious ``monitor_missing`` for a perfectly healthy
        # startup. Starting the monitor first closes that gap by construction.
        #
        # We start the monitor at the same logical point as before — AFTER the
        # node is running (``wait_until_ready`` proved ``trader.is_running``),
        # so the DataFreshnessActor exists and the registry has a live actor
        # feeding it. The production factory retrieves the actor via
        # ``node.trader.actors()``, injects the shared registry, derives the
        # required-feed universe, and returns the monitor. A legacy node (no
        # Databento feeds) STILL runs the monitor with an empty universe — it
        # publishes the EMPTY manifest. Gated on ``data_freshness_enabled`` so
        # a disabled deployment is a full no-op.
        #
        # Codex iter-2 P1 — FAIL-CLOSED on monitor-wiring failure. When
        # freshness is ENABLED but the factory/``start()`` raises (e.g. a bad
        # ``DATA_FRESHNESS_GRACE_JSON`` makes ``GraceConfig.from_env_json``
        # raise inside the factory, or the monitor's ``start()`` fails to open
        # its Redis client / publish the manifest), we must NOT let the node
        # keep trading with no freshness manifest and no data-stale
        # protection. The exception PROPAGATES — it is caught by the run-loop's
        # catch-all ``except`` (below, ~line 1548) which records
        # ``terminal_status="failed"`` + ``SPAWN_FAILED_PERMANENT`` + exit
        # code 1 and returns, so the node tears down through the SAME
        # finally/terminal path as any other startup failure and the
        # deployment row is marked ``failed``. Because this now runs BEFORE
        # ``_mark_running``, a monitor-wiring failure means the deployment is
        # NEVER marked ``running`` — strictly more fail-closed than before. We
        # do NOT latch the fleet halt here: a single node's config typo must
        # not halt OTHER healthy accounts. ``data_freshness_enabled=False`` is
        # the explicit opt-out (no factory call, no failure). NOTE: the monitor
        # is LIVE-ONLY — this entry point is never used in backtests, so it can
        # never be wired there.
        if freshness_enabled and data_freshness_monitor_factory is not None:
            data_stale_monitor = await _maybe_await(data_freshness_monitor_factory(payload, node))
            if data_stale_monitor is not None:
                # Review iter-16 P1: inject the SAME fail-closed local-shutdown
                # callback the IB disconnect handler gets (below). The monitor's
                # ``_fire_halt`` fires ``on_halt`` after a halt attempt REGARDLESS
                # of whether the Redis latch write succeeded — so an asymmetric
                # latch-WRITE exhaustion (the F6 read gate can't be trusted to
                # block in that case) still tears the node down locally. The
                # callback sets the local ``shutdown_requested`` event + stops the
                # node — purely in-process, no Redis. Injected BEFORE ``start()``
                # so it is armed before the first tick can fire a halt. Composes
                # with any pre-existing callback the factory configured; test
                # fakes that don't expose ``_on_halt`` are unaffected.
                if hasattr(data_stale_monitor, "_on_halt"):
                    _ds_preexisting_on_halt = data_stale_monitor._on_halt

                    async def _data_stale_local_shutdown_on_halt() -> None:
                        """Fail-closed fallback: set the local shutdown event +
                        stop the node. Runs AFTER any pre-existing on_halt the
                        factory configured."""
                        log.critical(
                            "data_stale_monitor_local_halt_triggered",
                            extra={
                                "deployment_id": str(payload.deployment_id),
                                "deployment_slug": payload.deployment_slug,
                                "reason": (
                                    "data feed stale past budget — triggering "
                                    "local shutdown regardless of Redis "
                                    "halt-latch write status"
                                ),
                            },
                        )
                        if _ds_preexisting_on_halt is not None:
                            with contextlib.suppress(Exception):
                                await _ds_preexisting_on_halt()
                        shutdown_requested.set()
                        with contextlib.suppress(Exception):
                            await node.stop_async()

                    data_stale_monitor._on_halt = _data_stale_local_shutdown_on_halt  # noqa: SLF001

                await data_stale_monitor.start()

        # Codex iter-22 P1 — STALE-AT-START checkpoint. ``data_stale_monitor.start()``
        # can fire the injected local-shutdown ``_on_halt`` SYNCHRONOUSLY: a
        # stale-at-start finding (a required feed already past budget on the very
        # first assert tick) halts + locally shuts down, which sets
        # ``shutdown_requested`` and schedules ``node.stop_async()``. Without this
        # checkpoint the run loop fell through to ``_mark_running`` (promote the
        # row to ``running``) + the reconciled-marker SET regardless — the only
        # ``shutdown_requested`` check was LATER (~before ``await node_run_task``).
        # Net: a halted, going-down node was recorded as a successfully running +
        # reconciled deployment, and ``/resume`` (which trusts the reconciled
        # marker for ACTIVE deployments) could clear the halt during the teardown
        # window. ``_mark_running``'s own stop-intent guard does NOT cover this —
        # that guard reads the DB row's ``stop_requested_at`` / ``status``, but a
        # LOCAL ``shutdown_requested`` event does not stamp the DB row. So we
        # short-circuit here: skip promotion + the marker SET entirely and fall
        # through to the existing teardown path (mirrors the later shutdown
        # check), leaving the marker absent → ``/resume`` fails closed.
        if shutdown_requested.is_set():
            log.warning(
                "data_stale_halt_at_start_skipping_promotion",
                extra={
                    "row_id": str(payload.row_id),
                    "deployment_id": str(payload.deployment_id),
                    "deployment_slug": payload.deployment_slug,
                },
            )
            node_run_task.cancel()
            _record_clean_exit("data_stale_halt_at_start")
            return terminal_exit_code

        # NOW flip the deployment row to ``running`` — AFTER the monitor has
        # published its manifest, so the ``running`` state implies the manifest
        # exists (no transient ``monitor_missing`` gap for healthy startups).
        promoted = await _mark_running(session_factory, payload.row_id)

        # PR 1b T4: SET the reconciled marker NOW — immediately AFTER
        # ``_mark_running`` succeeds (its contract is unchanged: readiness
        # implies a healthy reconcile, so the marker is SET only once the row
        # is ``running``). ``_mark_running`` is gated by ``wait_until_ready``
        # proving ``trader.is_running``, which in nautilus 1.223.0 cannot flip
        # True without a healthy reconciliation (``kernel.py:1025-1037``
        # returns early from ``start_async`` on a failed/timed-out reconcile so
        # ``trader.start()`` is never reached). We deliberately do NOT read
        # ``ExecutionEngine.reconciliation`` — that property is the config
        # FLAG, not a completion signal (``execution_engine.py:260``). The SET
        # lives at the run-loop level, NOT inside ``_mark_running`` (a DB-only
        # helper with no Redis/node handle). If readiness failed above we
        # already returned, so the marker stays absent (fail-closed).
        #
        # Codex iter-20 P1: gate the SET on ``_mark_running`` having ACTUALLY
        # PROMOTED the node row (``promoted is True``). ``_mark_running``
        # early-returns WITHOUT promoting when a concurrent ``/stop`` /
        # ``/kill-all`` raced startup and stamped stop intent on the node row
        # (or the row/deployment could not be resolved). Setting the marker in
        # that window would present a stop-raced, deliberately-unpromoted node
        # as ``reconciled`` — and ``/resume`` trusts the reconciled marker for
        # ACTIVE deployments (which include ``stopping``), so it could clear a
        # halt against a node that is going down. Skipping the SET keeps the
        # marker absent → ``/resume`` fails closed (no marker = refuse) for a
        # node being torn down, exactly as for a readiness failure.
        if reconciled_marker is not None and promoted:
            await reconciled_marker.mark_reconciled()

        # Phase 4 task 4.2 iter-2 wiring: spawn the IB disconnect
        # monitor as a sibling task. We start it AFTER the node is
        # running (not before) so the ``is_connected`` probe has a
        # valid data engine to call. The handler runs until
        # ``shutdown_requested`` is set, the grace window fires, or
        # the finally block cancels it. A failure to construct the
        # handler logs loudly but does NOT fail the deployment —
        # the supervisor's heartbeat watchdog is the fallback
        # safety net even with no disconnect monitor running.
        if disconnect_handler_factory is not None:
            try:
                disconnect_handler = await _maybe_await(disconnect_handler_factory(payload, node))
                if disconnect_handler is not None:
                    # Codex iter3 P2: local on_halt fallback.
                    #
                    # The handler's primary halt path is setting
                    # the Redis kill-switch flag which the
                    # supervisor watches. But the exact scenario
                    # ``IBDisconnectHandler`` was hardened for — an
                    # extended IB outage with correlated Redis
                    # trouble (network partition, datacenter
                    # issue) — means the Redis writes can fail AND
                    # the handler's retry loop exhausts. Without a
                    # local fallback, ``_fire_halt()`` would just
                    # log critical and exit, leaving the subprocess
                    # running with a dead order channel.
                    #
                    # Fix: inject an ``_on_halt`` callback that
                    # sets the local ``shutdown_requested`` event
                    # and schedules ``node.stop_async()``. These
                    # are purely in-process primitives — no Redis,
                    # no DB, no network. ``_fire_halt()`` runs the
                    # callback unconditionally (even when Redis
                    # writes failed, per ``disconnect_handler.py``
                    # Codex batch 10 P2 fix), so the subprocess
                    # always tears down when the grace window
                    # expires.
                    #
                    # The injection targets the private attribute
                    # so it composes with any on_halt the factory
                    # may have pre-configured (future extensibility).
                    # Test fakes that don't use ``_on_halt`` are
                    # unaffected — we check via ``hasattr``.
                    if hasattr(disconnect_handler, "_on_halt"):
                        _preexisting_on_halt = disconnect_handler._on_halt

                        async def _local_shutdown_on_halt() -> None:
                            """Fail-closed fallback: set the local
                            shutdown event + stop the node. Runs
                            AFTER any pre-existing on_halt the
                            factory configured."""
                            log.critical(
                                "ib_disconnect_handler_local_halt_triggered",
                                extra={
                                    "deployment_id": str(payload.deployment_id),
                                    "deployment_slug": payload.deployment_slug,
                                    "reason": (
                                        "grace window expired, triggering "
                                        "local shutdown regardless of Redis "
                                        "halt-flag write status"
                                    ),
                                },
                            )
                            if _preexisting_on_halt is not None:
                                with contextlib.suppress(Exception):
                                    await _preexisting_on_halt()
                            shutdown_requested.set()
                            with contextlib.suppress(Exception):
                                await node.stop_async()

                        disconnect_handler._on_halt = _local_shutdown_on_halt  # noqa: SLF001

                    disconnect_task = asyncio.create_task(
                        disconnect_handler.run(shutdown_requested),
                        name=f"ib_disconnect_handler-{payload.deployment_slug}",
                    )
            except Exception:  # noqa: BLE001
                log.exception(
                    "ib_disconnect_handler_spawn_failed",
                    extra={"deployment_id": str(payload.deployment_id)},
                )
                disconnect_handler = None
                disconnect_task = None

        if shutdown_requested.is_set():
            node_run_task.cancel()
            _record_clean_exit("before_node_run")
            return terminal_exit_code

        # Now wait for ``run_async`` to return — it blocks until
        # ``stop_async`` is called (which the SIGTERM handler
        # schedules) or an internal engine task fails.
        try:
            await node_run_task
        except asyncio.CancelledError:
            # Cleanly cancelled by the SIGTERM handler — fall
            # through to clean-exit recording.
            pass
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            log.exception(
                "trading_node_run_async_crashed",
                extra={
                    "row_id": str(payload.row_id),
                    "deployment_id": str(payload.deployment_id),
                },
            )
            terminal_status = "failed"
            # PR 2 / F2: this except fires only AFTER ``_mark_running`` above —
            # the node REACHED ``running`` and then ``node.run_async()`` raised.
            # That is a genuine RUNTIME crash the crash-recovery reaper / rescan
            # SHOULD re-drive (bounded by the RestartPolicy ceiling), so write
            # the recoverable NODE_CRASHED kind — NOT SPAWN_FAILED_PERMANENT,
            # which is reserved for pre-spawn / never-ran permanent failures
            # the recovery paths must leave for an operator START.
            terminal_failure_kind = FailureKind.NODE_CRASHED
            terminal_error = tb
            terminal_exit_code = 1
            return terminal_exit_code

        # Clean exit — record the outcome; the finally block will
        # run cleanup and THEN persist the terminal row.
        terminal_status = "stopped"
        terminal_failure_kind = FailureKind.NONE
        terminal_error = None
        terminal_exit_code = 0
        return terminal_exit_code

    except Exception as exc:  # noqa: BLE001 — catch-all so the finally block always runs
        tb = traceback.format_exc()
        log.exception(
            "trading_node_subprocess_failed",
            extra={
                "row_id": str(payload.row_id),
                "deployment_id": str(payload.deployment_id),
                "exc": str(exc),
            },
        )
        terminal_status = "failed"
        terminal_failure_kind = FailureKind.SPAWN_FAILED_PERMANENT
        terminal_error = tb
        terminal_exit_code = 1
        return terminal_exit_code

    finally:
        # Cleanup order (Codex batch 3 iter4 P1 fix):
        #
        # 1. ``node.stop_async()`` + ``dispose()`` run FIRST, with
        #    the heartbeat thread still alive. That keeps
        #    ``last_heartbeat_at`` advancing for the entire cleanup
        #    window, so if a slow dispose (IB socket teardown, Rust
        #    logger flush) exceeds ``HeartbeatMonitor``'s 30 s stale
        #    threshold, the monitor doesn't flip the still-live
        #    row to ``failed`` out from under us — which would
        #    drop the row out of the active-status set and let a
        #    concurrent ``/start`` reserve a new slot before this
        #    subprocess has released IB sockets + the Rust logger.
        #
        # 2. Heartbeat stops SECOND — only after the node is fully
        #    disposed and nothing else needs the row to stay fresh.
        #
        # 3. Terminal write LAST — row only drops out of the
        #    active-status set at this point. By this time the IB
        #    sockets + Rust logger are released, so a restart
        #    reserving the next slot is safe.
        # Dispose is conditionally skipped (Codex batch 3 iter11 P0
        # fix). When ``skip_dispose=True``, the production wrapper
        # handles dispose AFTER ``asyncio.run`` returns — Nautilus
        # 1.223.0 ``TradingNode.dispose()`` calls ``loop.stop()`` if
        # the kernel's loop is running, which is exactly the loop
        # ``asyncio.run`` is blocked on, and would break asyncio.run
        # with ``Event loop stopped before Future completed``.
        # Tests use the default ``False`` because their fake
        # ``dispose()`` is a no-op and the test loop is unaffected.
        # PR 1b T4: STOP the data-stale monitor first — it owns its own
        # background loop + Redis client (mirrors the IBDisconnectHandler
        # discipline). ``stop`` cancels the loop, DELETEs the per-feed JSON +
        # ``:verdict`` keys (iter-11 — so a fast restart on the stable
        # deployment id can't ``/resume`` off a prior run's stale ``warm``
        # verdicts), and closes its Redis client. The MANIFEST is deliberately
        # LEFT to TTL-expire (3x-tick), so a data-health scrape during this
        # still-'running' shutdown window never false-pages monitor_missing
        # (that pages off an ABSENT manifest, not off missing per-feed rows).
        # Deleting the per-feed keys runs BEFORE the terminal write below, so it
        # is impossible for this stop() to delete a NEWER same-deployment run's
        # keys — the new spawn cannot reserve the slot until the terminal write
        # drops this row out of the active-status set. It never
        # raises out (errors are swallowed), but we guard anyway so a monitor
        # teardown bug can never block node/heartbeat cleanup. NOTE: we do NOT
        # clear the reconciled marker here — an operator-resumable halt and a
        # clean stop are both expected to leave the marker's lifecycle to the
        # NEXT spawn's start-of-loop clear (restart re-arms fail-closed).
        if data_stale_monitor is not None:
            with contextlib.suppress(Exception):
                await data_stale_monitor.stop()

        # Close the reconciled marker's Redis client (if it owns one). The
        # marker is NOT cleared here — the NEXT spawn's start-of-loop clear
        # owns that, so a restart re-arms fail-closed.
        if reconciled_marker is not None:
            _marker_close = getattr(reconciled_marker, "aclose", None)
            if _marker_close is not None:
                with contextlib.suppress(Exception):
                    await _marker_close()

        # Cancel the disconnect handler task FIRST — it's a
        # sibling of ``node_run_task`` and should wind down before
        # ``node.stop_async()`` because the handler may still be
        # probing the data engine and we want to stop those probes
        # before the engine tears down. Phase 4 task 4.2 iter-2
        # wiring.
        if disconnect_task is not None:
            disconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await disconnect_task
        if disconnect_handler is not None:
            close_fn = getattr(disconnect_handler, "aclose", None)
            if close_fn is not None:
                try:
                    await close_fn()
                except Exception:  # noqa: BLE001
                    log.exception("ib_disconnect_handler_aclose_failed")

        if node is not None:
            try:
                await node.stop_async()
            except Exception:  # noqa: BLE001
                log.exception("trading_node_stop_async_failed")
            # Drain flatness-pending Redis list and publish stop_report:{nonce}
            # before dispose(). Wrapped in wait_for(5s) so a stuck Redis
            # never blocks shutdown (Bug #2, Codex iter-5 P1 #3).
            try:
                await asyncio.wait_for(
                    _drain_and_report_flatness(
                        node=node,
                        deployment_id=payload.deployment_id,
                        redis_url=payload.redis_url,
                    ),
                    timeout=5.0,
                )
            except TimeoutError:
                log.warning(
                    "flatness_drain_timeout",
                    extra={"deployment_id": str(payload.deployment_id)},
                )
            except Exception:  # noqa: BLE001
                log.exception("flatness_drain_failed")
            if not skip_dispose:
                with _safe_dispose(node):
                    pass

        if heartbeat is not None:
            try:
                heartbeat.stop()
                # Best-effort join with a short timeout so a stuck
                # thread doesn't wedge the subprocess's shutdown.
                if hasattr(heartbeat, "join"):
                    heartbeat.join(timeout=5.0)
            except Exception:  # noqa: BLE001
                log.exception("heartbeat_thread_stop_failed")

        # Terminal write happens LAST so the row only drops out of
        # the active-status set after IB sockets / Rust logger are
        # released. We swallow errors here so a DB blip during
        # shutdown doesn't escape as an unhandled exception —
        # the subprocess is already on the exit path.
        try:
            await _mark_terminal(
                session_factory,
                payload.row_id,
                status=terminal_status,
                failure_kind=terminal_failure_kind,
                error_message=terminal_error,
                exit_code=terminal_exit_code,
            )
        except Exception:  # noqa: BLE001
            log.exception("terminal_mark_failed")

        # Bug X2 fix: run cleanup callbacks (e.g. AsyncEngine.dispose)
        # INSIDE this event loop, after all other teardown completes.
        # The caller used to do ``asyncio.run(engine.dispose())`` from
        # the sync ``finally`` block after this coroutine returned,
        # which crashed with ``RuntimeError: ... attached to a
        # different loop`` + ``Event loop is closed`` because the
        # engine's connection pool was bound to THIS loop at create
        # time but a FRESH loop tried to dispose it. Doing the
        # dispose inside the original loop keeps binding + teardown
        # on the same event loop and avoids the cross-loop crash.
        if async_cleanup is not None:
            try:
                result = async_cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001
                log.exception("async_cleanup_failed")


# ---------------------------------------------------------------------------
# Context manager: swallow dispose errors so the terminal write path runs
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    """Support both sync and async factories. The
    ``disconnect_handler_factory`` hook can return either a
    ready :class:`IBDisconnectHandler` instance OR an
    awaitable that resolves to one (because constructing the
    handler in production requires opening an async Redis
    client, which is an async operation). Tests typically
    pass a sync stub; production passes an async builder.
    """
    if inspect.isawaitable(value):
        return await value
    return value


async def _drain_and_report_flatness(
    *,
    node: Any,
    deployment_id: UUID,
    redis_url: str,
) -> None:
    """Drain ``flatness_pending:{deployment_id}`` and write
    ``stop_report:{stop_nonce}`` for each ticket.

    Called from the run_trading_node finally block AFTER
    ``node.stop_async()`` resolves and BEFORE ``node.dispose()`` — at
    this point ``node.kernel.cache.positions_open()`` is still safe to
    call but Nautilus has run ``Strategy.stop()`` → ``market_exit()``
    so positions SHOULD be flat. Wrapped by the caller in
    ``asyncio.wait_for(5.0)`` so a stuck Redis never blocks shutdown.

    See ``docs/plans/2026-05-13-live-deploy-safety-trio.md`` §Bug #2.
    """
    if not redis_url:
        log.warning(
            "flatness_drain_no_redis_url",
            extra={"deployment_id": str(deployment_id)},
        )
        return

    import json as _json

    import redis.asyncio as aioredis

    client = aioredis.from_url(  # type: ignore[no-untyped-call]
        redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
    list_key = f"flatness_pending:{deployment_id}"
    try:
        while True:
            entry = await client.lpop(list_key)
            if entry is None:
                break
            try:
                ticket = _json.loads(entry)
            except (ValueError, TypeError):
                log.warning(
                    "flatness_ticket_unparsable",
                    extra={"deployment_id": str(deployment_id), "entry": entry},
                )
                continue
            stop_nonce = str(ticket.get("stop_nonce") or "")
            if not stop_nonce:
                log.warning(
                    "flatness_ticket_missing_nonce",
                    extra={"deployment_id": str(deployment_id)},
                )
                continue
            members = [str(x) for x in (ticket.get("member_strategy_id_fulls") or [])]
            cache_read_failed = False
            try:
                cache = node.kernel.cache
                all_open = cache.positions_open()
            except Exception:  # noqa: BLE001
                # PR #65 Codex P1: when the verification mechanism
                # itself fails, we MUST NOT report broker_flat=True.
                # Surface as non-flat with reason=cache_read_failed so
                # the operator knows positions could not be verified.
                log.exception("flatness_cache_read_failed")
                all_open = []
                cache_read_failed = True
            # Filter by member strategy_id_fulls — covers ALL members
            # of a portfolio (Codex iter-2 P1 #3 fix).
            my_open = [p for p in all_open if str(getattr(p, "strategy_id", "")) in members]
            broker_flat = (not my_open) and not cache_read_failed
            if cache_read_failed:
                reason = "cache_read_failed"
            elif my_open:
                reason = "max_attempts_exhausted"
            else:
                reason = "ok"
            report = {
                "stop_nonce": stop_nonce,
                "deployment_id": str(deployment_id),
                "broker_flat": broker_flat,
                "remaining_positions": [
                    {
                        "strategy_id": str(getattr(p, "strategy_id", "")),
                        "instrument_id": str(getattr(p, "instrument_id", "")),
                        "quantity": str(getattr(p, "quantity", "")),
                        "side": str(getattr(p, "side", "")),
                    }
                    for p in my_open
                ],
                "reason": reason,
                "reported_at": datetime.now(UTC).isoformat(),
            }
            # Per-nonce key, 120s TTL — coalesced API readers MUST be
            # able to GET the same key (no DEL race). See plan §Bug #2.
            await client.set(
                f"stop_report:{stop_nonce}",
                _json.dumps(report),
                ex=120,
            )
            log.info(
                "flatness_report_written",
                extra={
                    "deployment_id": str(deployment_id),
                    "stop_nonce": stop_nonce,
                    "broker_flat": report["broker_flat"],
                    "remaining_count": len(my_open),
                },
            )
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            log.exception("flatness_redis_aclose_failed")


@contextmanager
def _safe_dispose(node: Any) -> Iterator[None]:
    """Call ``node.dispose()`` swallowing any exception.

    Gotcha #20: ``dispose()`` must run on every code path to release
    the Rust-side logger and the IB sockets. If it raises, log and
    continue — the terminal-status write already happened (or is
    about to happen in the caller) and we don't want a dispose
    exception to mask the real failure reason.
    """
    try:
        yield
    finally:
        try:
            node.dispose()
        except Exception:  # noqa: BLE001
            log.exception("trading_node_dispose_failed")


async def real_data_freshness_monitor_factory(
    p: TradingNodePayload,
    node: Any,
) -> Any:
    """PR 1b T4: build the in-node :class:`DataStaleMonitor` for THIS node.

    Module-level (not nested in ``_trading_node_subprocess``) so the
    fail-closed wiring path can be exercised by the real factory in tests
    (Codex iter-2 P1) — in particular, the ``GraceConfig.from_env_json`` call
    below is INSIDE this factory, so a bad ``DATA_FRESHNESS_GRACE_JSON`` raises
    HERE and the run loop fails the subprocess fail-closed.

    Steps (all POST-build/start, mirroring the disconnect-handler factory):

    1. Build the shared :class:`FreshnessRegistry`.
    2. Retrieve the (single) :class:`DataFreshnessActor` from
       ``node.trader.actors()`` and inject the registry via
       ``set_registry`` so the actor's ``on_bar`` starts recording.
    3. Derive ``required_feeds`` from the SAME ``bar_type_datasets`` the
       actor config carries (``actor._fresh_config.bar_type_datasets``) so
       the manifest and the actor never drift. A LEGACY node (no
       DataFreshnessActor, empty map) yields ``required_feeds=set()`` — the
       monitor STILL runs and publishes the EMPTY manifest.
    4. Construct the monitor with its OWN ``decode_responses=False`` Redis
       client (mirroring ``_real_disconnect_handler_factory``), the node
       clock, and ``GraceConfig`` from the optional payload override.

    Returns ``None`` when ``redis_url`` is empty (no Redis → no monitor),
    which keeps tests that don't wire Redis on the legacy no-op path.
    """
    if not p.redis_url:
        return None

    import redis.asyncio as aioredis

    from msai.services.live.data_freshness import (
        FeedKey,
        FreshnessRegistry,
        GraceConfig,
        resolve_session_phase,
    )
    from msai.services.nautilus.data_freshness_actor import DataFreshnessActor
    from msai.services.nautilus.data_stale_monitor import DataStaleMonitor

    registry = FreshnessRegistry()

    # Retrieve the freshness actor + inject the registry. There is at most
    # one (appended by build_per_account_trading_node_config); a legacy
    # node has none, so the loop simply finds nothing and required_feeds
    # stays empty.
    bar_type_datasets: dict[str, str] = {}
    for actor in node.trader.actors():
        if isinstance(actor, DataFreshnessActor):
            actor.set_registry(registry)
            bar_type_datasets = dict(actor._fresh_config.bar_type_datasets)  # noqa: SLF001
            break

    required_feeds: set[FeedKey] = {
        FeedKey(dataset=dataset, native_bar_type_str=native_str)
        for native_str, dataset in bar_type_datasets.items()
    }

    # Derive node_id from the node's TraderId so the per-feed JSON +
    # cause attribution carry a stable, node-unique identity. Fall back to
    # the deployment slug if the trader_id isn't reachable.
    try:
        node_id = str(node.trader_id)
    except Exception:  # noqa: BLE001
        node_id = p.deployment_slug

    redis_url = p.redis_url

    def _redis_factory() -> Any:
        return aioredis.from_url(  # type: ignore[no-untyped-call]
            redis_url, decode_responses=False
        )

    # NOTE: this is the fail-closed point for a bad DATA_FRESHNESS_GRACE_JSON —
    # a raise here propagates to the run-loop catch-all and fails the node.
    cfg = GraceConfig.from_env_json(p.data_freshness_grace_json)

    return DataStaleMonitor(
        registry=registry,
        required_feeds=required_feeds,
        redis_factory=_redis_factory,
        cfg=cfg,
        phase_resolver=resolve_session_phase,
        deployment_id=str(p.deployment_id),
        account_id=p.ib_account_id,
        node_id=node_id,
        clock=node.kernel.clock,
    )


# ---------------------------------------------------------------------------
# Production entry point — top-level function so mp.Process can pickle it
# ---------------------------------------------------------------------------


def _trading_node_subprocess(payload: TradingNodePayload) -> NoReturn:
    """Pickle-safe top-level entry point for ``mp.get_context('spawn').Process``.

    Wires the real Nautilus node factory + a live async engine + a
    real heartbeat thread factory, then runs
    :func:`run_subprocess_async` inside ``asyncio.run``. The SIGTERM
    handler is registered **inside** ``run_subprocess_async`` (via
    ``loop.add_signal_handler``) so it can schedule
    ``node.stop_async()`` on the already-running async loop — a plain
    ``signal.signal`` handler would run in a foreign context and can't
    safely drive the async shutdown path (Codex batch 3 P1 fix).

    Gotcha #1: importing ``nautilus_trader`` installs uvloop as the
    event loop policy globally. Gotcha #18: ``asyncio.run(node.run())``
    would conflict. We reset the policy to ``None`` (default) first
    and then use ``asyncio.run`` on OUR wrapper, which manages its
    own loop.

    **Exit semantics** (Codex batch 3 iter4 P2 fix). The function
    terminates via ``sys.exit(exit_code)`` so ``mp.Process.exitcode``
    reflects the computed terminal outcome. Returning an ``int`` from
    an ``mp.Process`` target does NOT set the OS exit status —
    mp ignores the return value and the child exits 0 regardless.
    Without this, a handled failure whose terminal DB write missed
    (e.g. a transient DB blip inside the finally block) would reach
    ``FleetRouter.reap_once()`` with ``exitcode == 0`` and get
    misclassified as a clean ``stopped`` instead of the actual
    failure_kind we intended to write.

    **Node-crash child reaping (PR 2 T7).** The FIRST executable statement is
    :func:`_detach_session` — ``os.setsid()`` puts this child in its own session
    so a SIGTERM to the supervisor's process group never cascades into the node,
    and the child reparents cleanly to the container init (tini, via
    ``init: true``) on supervisor exit. ``mp.Process`` has no ``preexec_fn``, so
    this lives inside the target rather than as a spawn kwarg; it MUST stay first
    so it runs before any ``nautilus_trader`` import opens sockets or a signal
    arrives.
    """
    # PR 2 T7: detach into own session BEFORE anything else (no preexec_fn on
    # mp.Process — must be the first statement of the child entrypoint).
    _detach_session()

    # Gotcha #1 + #18
    asyncio.set_event_loop_policy(None)

    engine = create_async_engine(payload.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    def _real_heartbeat_factory(p: TradingNodePayload) -> _HeartbeatThread:
        # Codex batch 3 P1 fix: the real subprocess entry point MUST
        # construct a heartbeat thread. Without this, the heartbeat
        # stops moving after phase A and the HeartbeatMonitor stale
        # sweep kills any deployment that stays up past the 30s
        # threshold.
        return _HeartbeatThread(
            async_database_url=p.database_url,
            row_id=p.row_id,
        )

    async def _real_disconnect_handler_factory(
        p: TradingNodePayload,
        node: Any,
    ) -> Any:
        """Phase 4 task 4.2 iter-2 wiring: construct a real
        :class:`IBDisconnectHandler` bound to this node's data
        engine + the shared Redis. Returns ``None`` (no-op)
        when ``redis_url`` is empty, which keeps existing
        tests that don't care about disconnect monitoring
        working with the legacy fake subprocess path.

        The handler's ``aclose`` method is defined
        dynamically on the instance so ``run_subprocess_async``
        can close the Redis client without importing
        aioredis up here — the subprocess path keeps its
        imports narrow.
        """
        if not p.redis_url:
            return None
        import redis.asyncio as aioredis

        from msai.services.nautilus.disconnect_handler import (
            DEFAULT_GRACE_SECONDS,
            IBDisconnectHandler,
        )

        redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
            p.redis_url, decode_responses=False
        )

        def _is_connected() -> bool:
            # Probe BOTH engines' connectivity (Codex iter2 P1).
            # Nautilus opens SEPARATE IB clients for data
            # (``InteractiveBrokersDataClient`` with
            # ``ibg_data_client_id``) and exec
            # (``InteractiveBrokersExecutionClient`` with
            # ``ibg_exec_client_id``). If the exec client drops
            # but the data client stays up, the deployment has
            # market data but no working order channel — the
            # disconnect handler MUST treat that as an outage.
            #
            # Each engine's ``check_connected()`` iterates its own
            # registered clients and returns False if ANY client
            # is disconnected (data/engine.pyx:296,
            # execution/engine.pyx:similar). We AND them so either
            # layer failing trips the grace-window countdown. This
            # matches what ``startup_health.diagnose()`` already
            # does for the startup readiness check.
            try:
                data_ok = bool(node.kernel.data_engine.check_connected())
            except Exception:  # noqa: BLE001
                data_ok = False
            try:
                exec_ok = bool(node.kernel.exec_engine.check_connected())
            except Exception:  # noqa: BLE001
                exec_ok = False
            return data_ok and exec_ok

        handler = IBDisconnectHandler(
            redis=redis_client,
            is_connected=_is_connected,
            deployment_slug=p.deployment_slug,
            grace_seconds=DEFAULT_GRACE_SECONDS,
        )

        async def _aclose() -> None:
            with contextlib.suppress(Exception):
                await redis_client.aclose()

        handler.aclose = _aclose  # type: ignore[attr-defined]
        return handler

    async def _real_reconciled_marker_factory(p: TradingNodePayload) -> Any:
        """PR 1b T4: the reconciled-marker writer for THIS deployment.

        ``clear()`` DELETEs :func:`~msai.core.halt_keys.reconciled_key` at
        subprocess start (restart re-arms fail-closed); ``mark_reconciled()``
        SETs it (ISO-now, NO TTL) AFTER ``_mark_running`` proves a healthy
        reconcile. Owns its OWN ``decode_responses=False`` Redis client and
        ``aclose``s it when the run loop is done with it. Returns ``None``
        (no-op) when ``redis_url`` is empty.
        """
        if not p.redis_url:
            return None

        import redis.asyncio as aioredis

        from msai.core.halt_keys import reconciled_key

        redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
            p.redis_url, decode_responses=False
        )
        key = reconciled_key(str(p.deployment_id))

        class _ReconciledMarker:
            async def clear(self) -> None:
                await redis_client.delete(key)

            async def mark_reconciled(self) -> None:
                await redis_client.set(key, datetime.now(UTC).isoformat())

            async def aclose(self) -> None:
                with contextlib.suppress(Exception):
                    await redis_client.aclose()

        return _ReconciledMarker()

    # Codex batch 3 iter11 P0 fix: dispose() must run AFTER
    # ``asyncio.run`` exits, because Nautilus 1.223.0
    # ``TradingNode.dispose()`` calls ``loop.stop()`` on the kernel's
    # loop — which IS our ``asyncio.run`` loop, so calling dispose
    # from inside would crash with ``Event loop stopped before
    # Future completed``. We use a one-element list as an out-param
    # so ``run_subprocess_async`` can hand us back the constructed
    # node (it might be ``None`` if construction itself failed,
    # which is fine — nothing to dispose).
    node_box: list[Any] = []

    def _capture_node(n: Any) -> None:
        node_box.append(n)

    # Halt-gate cleanup box (F6 — PR 2 T2): the live ``on_post_build`` hook
    # below starts a background halt-refresh task + opens a Redis client. They
    # are cancelled/closed by ``_async_cleanup`` (which runs INSIDE the
    # asyncio.run loop, so the Redis pool tears down on its own loop).
    halt_refresh_box: dict[str, Any] = {"task": None, "redis": None}

    async def _wire_market_hours(
        node: Any, p: TradingNodePayload, sf: async_sessionmaker[AsyncSession]
    ) -> None:
        """Post-build hook (LIVE only — backtests have no such hook):

        1. ENFORCE the mandatory node-side halt gate (F6, REAL-MONEY P0): every
           live strategy MUST be a properly-ordered ``RiskAwareStrategy``, else
           raise → ``SPAWN_FAILED_PERMANENT`` BEFORE ``node.run_async()``.
        2. CONSTRUCT the OrderAuditWriter (used both for the node-side
           halt-denial audit injected onto each strategy AND for the
           engine-level msgbus hook).
        3. ARM the gate + inject the audit writer + start the background
           halt-refresh task (the one mandatory new collaborator), with an
           immediate awaited refresh so the cache is populated before the first
           bar. Arm-failure is LOUD + fail-closed (raise → SPAWN_FAILED_PERMANENT).
        4. Inject the MarketHoursService + engine-level audit hook.
        """
        # --- (1) Fail-closed enforcement FIRST. A non-halt-aware live strategy
        # never trades — raise BEFORE any further wiring or run_async(). The run
        # loop's catch-all maps this to FailureKind.SPAWN_FAILED_PERMANENT.
        enforce_halt_gate_mro(list(node.trader.strategies()))

        # --- (2) Construct the OrderAuditWriter OUTSIDE the swallowed best-effort
        # block below so an injection failure is visible, not silent. It is
        # injected onto each gated strategy's ``_audit`` (the ONLY path to record
        # a node-side halt denial — a BLOCKED order returns before
        # ``super().submit_order`` so no engine order event ever fires) AND reused
        # by the engine-level msgbus hook in step (4).
        from msai.services.nautilus.audit_hook import OrderAuditWriter

        writer = OrderAuditWriter(db=sf)  # keyword-only init (Codex fix)

        # --- (3) Arm the halt gate + inject the audit writer + start the refresh
        # task. Pass the ib_account_id (the DU…/U… latch id, per halt_keys) — NOT
        # ib_login_key. wire_halt_refresh arms FIRST + creates the recovery task
        # UNCONDITIONALLY, so a Redis blip during the immediate refresh fails
        # CLOSED (None cache → blocks opening orders) rather than disarming the
        # node. The ONLY way the gate ends up unarmed is a hard failure BEFORE
        # arming (e.g. Redis client construction throws). For a REAL-MONEY P0
        # surface that must be LOUD + fail-closed: re-raise so the run loop maps
        # it to SPAWN_FAILED_PERMANENT — a live node must NEVER trade with the
        # halt gate silently inert.
        # Per-strategy denial context (PR 2 T2): keyed on the Nautilus
        # ``StrategyId`` value (== the member's ``strategy_id_full``) so a
        # node-side halt block can write a COMPLETE ``denied`` row. Built from
        # the SAME member identity the engine-level msgbus hook uses
        # (``strategy_id`` / ``strategy_code_hash`` per member; ``deployment_id``
        # from the node payload). A member with a missing identity is simply
        # omitted → that strategy falls back to the best-effort UPDATE-only path
        # rather than getting fabricated identity.
        from msai.services.nautilus.risk import DenialContext

        _denial_contexts: dict[str, Any] = {}
        for _m in p.strategy_members:
            if _m.strategy_id_full and _m.strategy_id is not None:
                _denial_contexts[_m.strategy_id_full] = DenialContext(
                    deployment_id=p.deployment_id,
                    strategy_id=_m.strategy_id,
                    strategy_code_hash=_m.strategy_code_hash
                    or p.strategy_code_hash
                    or "engine-audit",
                    strategy_git_sha=None,
                )

        if p.redis_url:
            import redis.asyncio as _aioredis

            redis_client = _aioredis.from_url(  # type: ignore[no-untyped-call]
                p.redis_url,
                decode_responses=False,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            halt_refresh_box["redis"] = redis_client
            halt_refresh_box["task"] = await wire_halt_refresh(
                strategies=list(node.trader.strategies()),
                redis_client=redis_client,
                account_id=p.ib_account_id,
                audit_writer=writer,
                denial_contexts=_denial_contexts,
            )
        else:
            # No Redis URL → the halt gate cannot be armed. For a live node this
            # is a misconfiguration that must fail LOUD + fail-closed (never trade
            # without node-side halt enforcement), NOT a silent degrade.
            raise RuntimeError(
                "live trading node has no redis_url — the node-side halt gate "
                "(F6, REAL-MONEY P0) cannot be armed; refusing to start. "
                "SPAWN_FAILED_PERMANENT."
            )

        # --- (4) MarketHoursService + audit hook (best-effort).
        try:
            from msai.services.nautilus.market_hours import (
                MarketHoursService,
                make_market_hours_check,
            )
            from msai.services.nautilus.risk import RiskAwareStrategy

            svc = MarketHoursService()
            # Prime with canonical instrument IDs (e.g. "AAPL.NASDAQ").
            # paper_symbols contains bare tickers ("AAPL") which don't
            # match instrument_cache.canonical_id. Codex review P1 fix.
            instrument_ids = list(p.canonical_instruments) if p.canonical_instruments else []
            if instrument_ids:
                async with sf() as session:
                    await svc.prime(session, instrument_ids)

            check = make_market_hours_check(svc)

            # Inject into every strategy that is a RiskAwareStrategy
            for strategy in node.trader.strategies():
                if isinstance(strategy, RiskAwareStrategy):
                    strategy._market_hours_check = check  # noqa: SLF001
        except Exception:  # noqa: BLE001
            log.warning("market_hours_wiring_failed")

        # Engine-level audit hook: subscribe to ALL order events via
        # the message bus so every order is audited regardless of
        # whether the strategy uses RiskAwareStrategy or not.
        # Topic pattern: events.order.{strategy_id}
        try:
            import asyncio as _aio
            from datetime import UTC, datetime
            from decimal import Decimal

            from msai.services.nautilus.audit_hook import (
                OrderSubmittedFacts,
                TradeFillFacts,
            )

            # ``writer`` was constructed in step (2) and is reused here for the
            # engine-level msgbus audit hook (the node-side halt-denial audit was
            # already injected onto each strategy's ``_audit`` via step (3)).
            _cache = node.kernel.cache  # for fetching full order details
            _loop = _aio.get_running_loop()

            _strategy_id_lookup: dict[str, UUID] = (
                {m.strategy_id_full: m.strategy_id for m in p.strategy_members}
                if p.strategy_members
                else {}
            )

            def _resolve_strategy_id(event: Any) -> UUID:
                if _strategy_id_lookup:
                    nautilus_sid = str(getattr(event, "strategy_id", ""))
                    if nautilus_sid in _strategy_id_lookup:
                        return _strategy_id_lookup[nautilus_sid]
                return p.strategy_id or p.deployment_id

            def _on_order_event_sync(event: Any) -> None:
                """Sync handler bridging to async audit writer.

                The Nautilus msgbus calls handlers synchronously from
                the Cython event loop. We schedule the async DB write
                as a fire-and-forget task on the running loop.
                """
                event_type = type(event).__name__

                async def _write() -> None:
                    try:
                        if event_type == "OrderSubmitted":
                            # OrderSubmitted doesn't carry side/qty/price —
                            # fetch the full order from Nautilus cache (Codex fix)
                            order = _cache.order(event.client_order_id)
                            _side = str(order.side) if order else "UNKNOWN"
                            _qty = Decimal(str(order.quantity)) if order else Decimal("0")
                            _price = None
                            _order_type = str(order.order_type) if order else "UNKNOWN"
                            _instrument = (
                                str(order.instrument_id) if order else str(event.instrument_id)
                            )

                            await writer.write_submitted(
                                OrderSubmittedFacts(
                                    client_order_id=str(event.client_order_id),
                                    strategy_id=_resolve_strategy_id(event),
                                    strategy_code_hash=p.strategy_code_hash or "engine-audit",
                                    instrument_id=_instrument,
                                    side=_side,
                                    quantity=_qty,
                                    price=_price,
                                    order_type=_order_type,
                                    ts_attempted=datetime.now(UTC),
                                    deployment_id=p.deployment_id,
                                    is_live=True,
                                )
                            )
                        elif event_type == "OrderFilled":
                            # Phase 2 #4 port + Codex review P1/P2:
                            # 1. write_trade_fill first — idempotent via
                            #    (deployment_id, broker_trade_id) partial
                            #    unique index. If the fill is a replay
                            #    (reconciliation — nautilus.md gotcha 19
                            #    — or a redelivered msgbus event), this
                            #    returns False so downstream metric +
                            #    audit work is suppressed.
                            # 2. Only flip the audit row AND increment
                            #    ORDERS_FILLED when a genuinely new fill
                            #    was persisted. Reviewer objection P2:
                            #    keeping ORDERS_FILLED inside
                            #    update_filled inflated the counter on
                            #    every warm-restart reconciliation pass.
                            # 3. If the audit row is missing at
                            #    update_filled time, an OrderSubmitted
                            #    task is still in flight (asyncio race —
                            #    create_task doesn't guarantee ordering)
                            #    OR we never saw the submit for this
                            #    fill (cold-reconciliation path). Log a
                            #    WARN with enough context to find the
                            #    orphaned audit row later; the Trade row
                            #    itself is the authoritative record of
                            #    the broker-side execution.
                            _trade_persisted = False
                            if p.deployment_id is not None:
                                _last_px = getattr(event, "last_px", None)
                                _last_qty = getattr(event, "last_qty", None)
                                _commission = getattr(event, "commission", None)
                                _ts_event_ns = getattr(event, "ts_event", None)
                                _executed_at = (
                                    datetime.fromtimestamp(_ts_event_ns / 1_000_000_000, UTC)
                                    if _ts_event_ns
                                    else datetime.now(UTC)
                                )
                                # Nautilus's OrderSide enum stringifies as its
                                # int value (``"1"`` / ``"2"``) on this version,
                                # NOT as ``"BUY"`` / ``"SELL"``. Drill 2026-04-15
                                # surfaced trades with ``side="1"`` in the DB.
                                # Map by name when available, fall back to the
                                # int→string mapping, then to the raw stringify
                                # so future enum changes still produce something.
                                _raw_side = getattr(event, "order_side", None)
                                _name = getattr(_raw_side, "name", None)
                                if _name:
                                    _order_side = _name
                                else:
                                    _side_str = (
                                        str(_raw_side) if _raw_side is not None else "UNKNOWN"
                                    )
                                    _int_to_name = {"1": "BUY", "2": "SELL"}
                                    _order_side = _int_to_name.get(_side_str, _side_str)
                                    # Strip ``OrderSide.BUY`` -> ``BUY`` for older reprs.
                                    if "." in _order_side:
                                        _order_side = _order_side.rsplit(".", 1)[-1]
                                _trade_persisted = await writer.write_trade_fill(
                                    TradeFillFacts(
                                        broker_trade_id=str(event.trade_id),
                                        client_order_id=str(event.client_order_id),
                                        deployment_id=p.deployment_id,
                                        strategy_id=_resolve_strategy_id(event),
                                        strategy_code_hash=p.strategy_code_hash or "engine-audit",
                                        instrument=str(event.instrument_id),
                                        side=_order_side,
                                        quantity=(
                                            Decimal(str(_last_qty))
                                            if _last_qty is not None
                                            else Decimal("0")
                                        ),
                                        price=(
                                            Decimal(str(_last_px))
                                            if _last_px is not None
                                            else Decimal("0")
                                        ),
                                        commission=(
                                            Decimal(str(_commission).split()[0])
                                            if _commission is not None
                                            else None
                                        ),
                                        executed_at=_executed_at,
                                    )
                                )
                            if _trade_persisted:
                                _audit_updated = await writer.update_filled(
                                    str(event.client_order_id)
                                )
                                if not _audit_updated:
                                    print(  # noqa: T201 — structlog not wired in subprocess
                                        f"[MSAI] OrderFilled for {event.client_order_id!s} "
                                        "had no matching audit row; Trade persisted but "
                                        "order_attempt_audits is stuck at pre-fill status "
                                        "(asyncio race or cold-reconciliation replay).",
                                        flush=True,
                                    )
                                from msai.services.observability.trading_metrics import (
                                    ORDERS_FILLED,
                                )

                                ORDERS_FILLED.inc()
                        elif event_type == "OrderAccepted":
                            broker_id = (
                                str(event.venue_order_id)
                                if hasattr(event, "venue_order_id")
                                else None
                            )
                            await writer.update_accepted(
                                str(event.client_order_id), broker_order_id=broker_id
                            )
                        elif event_type == "OrderCanceled":
                            await writer.update_cancelled(str(event.client_order_id), reason=None)
                        elif event_type == "OrderRejected":
                            reason = str(event.reason) if hasattr(event, "reason") else None
                            await writer.update_rejected(str(event.client_order_id), reason=reason)
                            # PR 1b T7: if the rejection is an IB EXEC-side
                            # pacing/throttle, INCR the per-account counter the
                            # data-health API hydrates into IB_EXEC_PACING_ERRORS.
                            # Reuse the node's halt-gate Redis client (the only
                            # client in scope at this hook); swallows its own
                            # errors so a counter blip never drops the audit.
                            _pacing_redis = halt_refresh_box.get("redis")
                            if _pacing_redis is not None:
                                await record_ib_exec_pacing(
                                    _pacing_redis,
                                    reason=reason,
                                    account_id=p.ib_account_id,
                                )
                    except Exception as _evt_exc:  # noqa: BLE001
                        print(f"[MSAI] Audit event {event_type} FAILED: {_evt_exc!r}", flush=True)  # noqa: T201

                _loop.create_task(_write())

            # Subscribe to ALL order events via wildcard pattern
            node.kernel.msgbus.subscribe(topic="events.order.*", handler=_on_order_event_sync)

            # Position events — PositionClosed carries realized_pnl
            # that we need to write back to the closing Trade row so
            # the ``pnl`` column in the DB is populated.
            def _on_position_event_sync(event: Any) -> None:
                event_type = type(event).__name__
                if event_type != "PositionClosed":
                    return

                async def _write_pnl() -> None:
                    try:
                        closing_id = str(event.closing_order_id)
                        rpnl = getattr(event, "realized_pnl", None)
                        if rpnl is not None and p.deployment_id is not None:
                            pnl_str = str(rpnl).split()[0]
                            await writer.update_trade_pnl(
                                closing_order_id=closing_id,
                                deployment_id=p.deployment_id,
                                realized_pnl=Decimal(pnl_str),
                            )
                    except Exception as _pnl_exc:  # noqa: BLE001
                        print(  # noqa: T201
                            f"[MSAI] PositionClosed PnL write FAILED: {_pnl_exc!r}",
                            flush=True,
                        )

                _loop.create_task(_write_pnl())

            node.kernel.msgbus.subscribe(topic="events.position.*", handler=_on_position_event_sync)
            # Use print() because structlog isn't initialized in the subprocess
            print(  # noqa: T201
                "[MSAI] Engine-level audit hook wired via events.order.* + events.position.*",
                flush=True,
            )
        except Exception as _audit_exc:  # noqa: BLE001
            print(f"[MSAI] Engine audit hook wiring FAILED: {_audit_exc!r}", flush=True)  # noqa: T201

    async def _async_cleanup() -> None:
        """Tear down the halt-refresh task + its Redis client (F6 — PR 2 T2)
        FIRST, then dispose the AsyncEngine. Runs INSIDE the asyncio.run loop so
        both the Redis pool and the engine pool tear down on their own loop."""
        task = halt_refresh_box.get("task")
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        redis_client = halt_refresh_box.get("redis")
        if redis_client is not None:
            with contextlib.suppress(Exception):
                await redis_client.aclose()
        await engine.dispose()

    # Bug X2 fix: dispose the AsyncEngine INSIDE the same event loop
    # that created its connection pool. Previously the dispose ran
    # via a second ``asyncio.run(engine.dispose())`` call after the
    # first ``asyncio.run`` loop had torn down — that created a
    # fresh loop whose tasks couldn't safely close pool connections
    # bound to the original loop, resulting in
    # ``RuntimeError: ... attached to a different loop`` +
    # ``Event loop is closed`` on every clean shutdown. Handing the
    # dispose in via ``async_cleanup`` keeps everything on one loop.
    exit_code = asyncio.run(
        run_subprocess_async(
            payload,
            session_factory=session_factory,
            node_factory=_build_real_node,
            heartbeat_factory=_real_heartbeat_factory,
            disconnect_handler_factory=_real_disconnect_handler_factory,
            install_signal_handlers=True,
            skip_dispose=True,
            on_node_constructed=_capture_node,
            on_post_build=_wire_market_hours,
            async_cleanup=_async_cleanup,
            data_freshness_monitor_factory=real_data_freshness_monitor_factory,
            reconciled_marker_factory=_real_reconciled_marker_factory,
        )
    )

    # Dispose the Nautilus node from sync context — its kernel loop
    # is no longer running (asyncio.run already exited) so dispose
    # won't try to stop a live loop. Gotcha #20 (must dispose to
    # release Rust logger + IB sockets) is satisfied here, not in
    # the finally above.
    if node_box:
        node = node_box[0]
        try:
            node.dispose()
        except Exception:  # noqa: BLE001
            log.exception("trading_node_dispose_failed_post_loop")

    # Propagate the computed terminal code to the OS so
    # ``mp.Process.exitcode`` matches ``terminal_exit_code``. Must be
    # ``sys.exit`` — an ``mp.Process`` target's return value is
    # discarded.
    sys.exit(exit_code)


def _build_real_node(payload: TradingNodePayload) -> Any:
    """Production node factory — constructs a real Nautilus TradingNode.

    Imports are deferred inside the function so test invocations of
    :func:`run_subprocess_async` (with fake factories) never pay
    Nautilus's multi-second import cost. Under the ``mp.Process``
    spawn context this function runs in a fresh interpreter anyway
    so module-level vs function-level imports only matter for tests.

    Steps (Nautilus 1.223.0 blessed pattern, live/node.py:230-281):

    1. Build a ``TradingNodeConfig`` from the payload via
       :func:`build_live_trading_node_config`. The config already
       wires ``data_clients[IB_VENUE.value]`` /
       ``exec_clients[IB_VENUE.value]`` with
       :class:`InteractiveBrokersDataClientConfig` /
       :class:`InteractiveBrokersExecClientConfig` instances.
    2. Construct ``TradingNode(config)``. The node's
       ``TradingNodeBuilder`` captures the current asyncio loop
       during ``__init__`` — we rely on the caller
       (:func:`run_subprocess_async` via ``asyncio.run``) to be
       on the loop thread.
    3. Register the two IB client factories against the ``"INTERACTIVE_BROKERS"``
       key. The key MUST match ``IB_VENUE.value`` — the name that
       :func:`build_live_trading_node_config` used when adding the
       client configs to the ``data_clients`` / ``exec_clients``
       dicts. A mismatch surfaces as "no factory for client X" at
       ``node.build()`` time.

    Gotchas honored:

    - **#3** (unique ``client_id``): ``build_live_trading_node_config``
      derives distinct ``ibg_data_client_id`` /
      ``ibg_exec_client_id`` from the deployment slug so two
      concurrent subprocesses can't silently steal each other's
      IB connections.
    - **#4** (venue name pinning): the config uses ``IB_VENUE``
      (``"INTERACTIVE_BROKERS"``) consistently; we register the
      factories under the same name here.
    - **#6** (port/account consistency): validated inside
      ``build_live_trading_node_config`` via
      ``_validate_port_account_consistency``.
    - **#10** (reconciliation on startup): already set via
      ``LiveExecEngineConfig(reconciliation=True)`` in the config
      builder; we do NOT override it here.
    - **#18** (``asyncio.run`` loop conflict): we do NOT call
      ``node.run()`` — :func:`run_subprocess_async` drives
      ``node.run_async()`` as a scheduled task on the already-
      running ``asyncio.run`` loop.
    """
    from nautilus_trader.adapters.interactive_brokers.common import IB
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveDataClientFactory,
        InteractiveBrokersLiveExecClientFactory,
    )
    from nautilus_trader.live.node import TradingNode

    from msai.services.nautilus.live_node_config import (
        IBSettings,
        build_live_trading_node_config,
        build_per_account_strategy_configs,
        build_per_account_trading_node_config,
        build_portfolio_trading_node_config,
    )

    ib_settings = IBSettings(
        host=payload.ib_host,
        port=payload.ib_port,
        account_id=payload.ib_account_id,
    )

    # Decode ``spawn_today_iso`` into a ``date`` so the provider config
    # builder uses the exact same front-month the supervisor used when
    # canonicalizing the strategy's ``instrument_id``. Empty/invalid
    # strings fall back to the subprocess's own clock (back-compat).
    from datetime import date as _date

    spawn_today: _date | None = None
    if payload.spawn_today_iso:
        try:
            spawn_today = _date.fromisoformat(payload.spawn_today_iso)
        except ValueError:
            spawn_today = None

    # PR 1 T12: per-account split topology (Databento data + IB exec).
    # When the supervisor's async payload factory pre-resolved the
    # Databento targets + the canonical → native bar-type map,
    # route to :func:`build_per_account_trading_node_config` and
    # register the Databento data-client factory + IB exec factory.
    # When any of the three pre-resolved fields is empty we keep the
    # legacy data+exec wiring so existing tests (and any deployment
    # the supervisor hasn't migrated yet) stay green.
    if payload.use_per_account_topology:
        from nautilus_trader.adapters.databento.constants import DATABENTO_CLIENT_ID
        from nautilus_trader.adapters.databento.factories import (
            DatabentoLiveDataClientFactory,
        )

        from msai.core.config import settings as _settings
        from msai.services.nautilus.ibg_client_id import (
            ROLE_EXEC,
            derive_ibg_client_id,
        )

        # ``ibg_client_id`` is supplied by the supervisor (single source
        # of truth via :func:`derive_ibg_client_id`). Fall back to the
        # SYNC builder's own legacy derivation when the supervisor
        # passed 0 — happens only in tests that bypass the supervisor
        # payload factory.
        ibg_client_id = payload.ibg_client_id or derive_ibg_client_id(
            payload.deployment_slug, ROLE_EXEC
        )

        # Codex iter 1 P1-1 + P1-3 of PR 1 — build ``ImportableStrategyConfig``s
        # from the supervisor's strategy_members BEFORE handing them to
        # the per-account builder. Without this the spawned TradingNode
        # contained the shim actor but ZERO strategies (P1-1); without
        # the venue rewrite each strategy subscribed under ``.NASDAQ``
        # while the shim republishes onto ``.IBKR`` so no bars ever
        # reached the strategy (P1-3). ``build_per_account_strategy_configs``
        # threads the rewrite + matches the legacy portfolio loop's
        # ``order_id_tag`` / ``manage_stop`` / US-equity TIF wiring.
        per_account_strategy_configs = build_per_account_strategy_configs(
            payload.strategy_members,
            deployment_slug=payload.deployment_slug,
        )

        # F2 fix (Codex iter 2 P1): preload the IB exec instrument
        # provider with REAL ``IBContract`` objects keyed by their
        # LISTING venue (NASDAQ/NYSE/etc.), NOT canonical ``.IBKR`` ids.
        # The resolver's ``ResolvedInstrument`` rows already carry the
        # full ``contract_spec`` (mirrors the legacy portfolio path).
        # De-duplicate by canonical_id to avoid two strategies on the
        # same instrument producing two IBContract entries.
        _seen_resolved: dict[str, ResolvedInstrument] = {}
        for _m in payload.strategy_members:
            for _ri in _m.resolved_instruments:
                _seen_resolved.setdefault(_ri.canonical_id, _ri)
        per_account_resolved = list(_seen_resolved.values())

        config = build_per_account_trading_node_config(
            account_id=payload.ib_account_id,
            ibg_client_id=ibg_client_id,
            ib_login_key="",  # PR 1: surfaced for audit symmetry only — actor ignores it
            native_instrument_ids=list(payload.native_instrument_ids),
            venue_dataset_map=dict(payload.venue_dataset_map),
            canonical_to_native_bar_types=dict(payload.canonical_to_native_bar_types),
            databento_api_key=_settings.databento_api_key,
            ib_host=payload.ib_host,
            ib_port=payload.ib_port,
            deployment_slug=payload.deployment_slug,
            strategies=per_account_strategy_configs,
            resolved_instruments=per_account_resolved,
        )

        node = TradingNode(config=config)
        # Databento OWNS data; IB OWNS exec only. Register BOTH factories.
        # ``DATABENTO_CLIENT_ID`` is the module-level ``ClientId("DATABENTO")``
        # constant; ``add_data_client_factory`` takes the bare name string.
        node.add_data_client_factory(str(DATABENTO_CLIENT_ID), DatabentoLiveDataClientFactory)
        node.add_exec_client_factory(IB, InteractiveBrokersLiveExecClientFactory)
        return node

    # Multi-strategy path: when strategy_members is populated, build a
    # portfolio config with N strategies sharing a single exec client.
    # Otherwise fall through to the legacy single-strategy path.
    if payload.strategy_members:
        config = build_portfolio_trading_node_config(
            deployment_slug=payload.deployment_slug,
            strategy_members=payload.strategy_members,
            ib_settings=ib_settings,
            spawn_today=spawn_today,
        )
    else:
        config = build_live_trading_node_config(
            deployment_slug=payload.deployment_slug,
            strategy_path=payload.strategy_path,
            strategy_config_path=payload.strategy_config_path,
            strategy_config=payload.strategy_config,
            paper_symbols=payload.paper_symbols,
            ib_settings=ib_settings,
            spawn_today=spawn_today,
        )

    node = TradingNode(config=config)
    # ``IB`` is the module-level constant ``"INTERACTIVE_BROKERS"``
    # (nautilus_trader/adapters/interactive_brokers/common.py:32).
    # Using the named constant instead of a string literal keeps
    # us insulated from a Nautilus rename.
    node.add_data_client_factory(IB, InteractiveBrokersLiveDataClientFactory)
    node.add_exec_client_factory(IB, InteractiveBrokersLiveExecClientFactory)
    return node
