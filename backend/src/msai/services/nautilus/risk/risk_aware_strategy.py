"""RiskAwareStrategy mixin (Phase 3 task 3.7).

Per the natives audit and Codex finding #2: Nautilus's
``LiveRiskEngine`` is **not subclassable** via config — the only
way to plug custom risk logic into the live order path is via
a Strategy mixin that runs checks BEFORE calling
``submit_order``. The built-in ``LiveRiskEngine`` (configured
in Task 3.8) still runs AFTER this mixin, so we get
defense-in-depth: this mixin enforces per-deployment limits
(daily loss, max position, exposure, market hours, halt flag),
and Nautilus's engine enforces precision / native max-notional
/ rate limits.

Portfolio API gotcha (Codex v3 P1)
----------------------------------

Nautilus's ``Portfolio`` has both per-instrument and per-venue
accessors with **different names**:

+----------------+--------------------------+--------------------------+----------------------+
| Scope          | PnL                      | Exposure                 | Returns              |
+================+==========================+==========================+======================+
| Per-instrument | ``total_pnl``            | ``net_exposure``         | ``Money | None``     |
+----------------+--------------------------+--------------------------+----------------------+
| Per-venue      | ``total_pnls`` (plural!) | ``net_exposures``        | ``dict[Currency,     |
| (aggregate)    |                          | (plural!)                | Money]``             |
+----------------+--------------------------+--------------------------+----------------------+

The plurals take a ``Venue``, the singulars take an
``InstrumentId``. v3 of this plan called ``portfolio.total_pnl(venue)``
which silently returned ``None`` because Nautilus's signature
is ``total_pnl(InstrumentId, ...)`` and a ``Venue`` doesn't
match — so the daily-loss check was a no-op. v4+ uses the
plurals for venue aggregates.

Verified against Nautilus 1.223.0 ``portfolio/portfolio.pyx``::

    cpdef dict total_pnls(self, Venue venue=None, ...)        # line 958
    cpdef dict net_exposures(self, Venue venue=None, ...)     # line 1008
    cpdef Money total_pnl(self, InstrumentId instrument_id, ...)  # line 1197
    cpdef Money net_exposure(self, InstrumentId instrument_id, ...)  # line 1256
    cpdef object net_position(self, InstrumentId instrument_id, ...) # line 1584

Node-side live-halt order gate (PR 2 T2 / F6 — REAL-MONEY P0)
-------------------------------------------------------------

The supervisor-driven kill-all (SIGTERM + push-stop + Redis latch re-check at
spawn) is still the primary mechanism. ON TOP of it, this mixin OVERRIDES the
public submit cpdefs so EVERY live strategy gets a node-side halt gate that is
router-independent — it reads the fleet + per-account halt latches directly from
Redis (via a ≤1s background-refreshed sync cache) and BLOCKS any new opening
order while either latch is set, even if the supervisor is down. ``None`` /
stale (> ``HALT_CACHE_MAX_AGE_S``) / ``True`` fails CLOSED for new exposure.

This is NOT the old "cached flag with one-bar lag" defense-in-depth note: the
gate is the FAST node-local layer (bounded staleness ≤2s) that closes the F6
hole where a strategy calling ``self.submit_order`` directly got zero node-level
halt enforcement. Reduce-only / ``MARKET_EXIT`` flatten orders are ALWAYS allowed
so kill-all/drain can still flatten under a halt. The gate is armed only in
live (the live ``on_post_build`` wirer sets ``_halt_gate_armed``); backtests
never reach that hook, so the branch stays inert and never blocks a backtest.
See the :class:`RiskAwareStrategy` docstring for the full per-order semantics.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from msai.services.nautilus.audit_hook import OrderAuditWriter


log = logging.getLogger(__name__)


HALT_CACHE_MAX_AGE_S: float = 2.0
"""Maximum age (seconds) of the live halt cache before it is treated as STALE.

The node-side halt gate (F6) is synchronous (the ``on_bar`` hot path), so it
reads a cache fed by a background asyncio task instead of doing a blocking Redis
GET inline. A cache older than this is treated as a halt (fail-closed for new
exposure) — a Redis blip that starves the refresh task must NOT silently let new
opening orders through. ``2.0s`` is comfortably above the refresh task's ``≤1s``
poll interval, so a healthy fleet never trips the staleness branch."""


@dataclass(frozen=True)
class RiskLimits:
    """Per-deployment risk limits the mixin enforces. All
    monetary values are in USD; the mixin asks Nautilus to
    convert via ``target_currency=USD`` so multi-currency
    accounts collapse to a single comparable scalar.

    Limits are loaded from the ``live_deployments`` row at
    deployment-start time (Phase 1 Task 1.14 wrote them) and
    passed verbatim to the strategy via the strategy config.
    """

    daily_loss_limit_usd: Decimal
    """If the venue's net total PnL across all currencies (in
    USD) is below ``-daily_loss_limit_usd``, refuse any new
    orders. Default to a large positive number to disable."""

    max_notional_exposure_usd: Decimal
    """If projected venue exposure (current + new order
    notional, in USD) exceeds this, refuse the order. The
    "projected" form means we don't accept a new order that
    would put us over the limit, even if we're currently
    under."""

    max_position_per_instrument: Decimal
    """Absolute net position cap per instrument. Compared
    against the projected position after the order would
    fill. The mixin signs the order quantity by side
    (BUY positive, SELL negative) before adding."""


@dataclass(frozen=True)
class DenialContext:
    """Per-run identity the strategy needs to write a COMPLETE
    ``denied`` audit row when the node-side halt gate blocks an
    opening order (PR 2 T2 audit-completeness fix).

    A node-side halt block returns BEFORE the order reaches Nautilus,
    so no ``OrderSubmitted`` event fires and the engine-level audit
    hook never inserts a ``submitted`` row. To durably record the
    denial (``nautilus.md`` #10 — every order attempt on the
    real-money path must be auditable), :meth:`RiskAwareStrategy.
    _record_denial` needs the same per-run identity the engine hook
    carries (``deployment_id`` / ``strategy_id`` /
    ``strategy_code_hash``) PLUS the blocked order's own fields.

    Wired onto each gated strategy by the live ``on_post_build``
    hook, alongside ``_audit``. ``None`` (the class default) means
    "context not wired" — :meth:`_record_denial` then falls back to
    the best-effort UPDATE-only :meth:`OrderAuditWriter.update_denied`
    rather than fabricating placeholder identity into a real-money
    audit row (a wrong strategy_id/deployment_id is worse than a
    logged-only denial).
    """

    deployment_id: Any
    """``UUID`` FK to ``live_deployments.id`` for this node."""
    strategy_id: Any
    """``UUID`` FK to ``strategies.id`` for THIS strategy member."""
    strategy_code_hash: str
    """SHA256 of the strategy file at deployment-start time."""
    strategy_git_sha: str | None = None
    """Optional 40-char git SHA when the strategy lives in a git checkout."""


@dataclass(frozen=True)
class RiskCheckResult:
    """The outcome of one ``submit_order_with_risk_check`` call.
    A small dataclass so callers (and tests) can inspect why
    an order was denied without parsing log lines."""

    allowed: bool
    reason: str | None = None
    """When ``allowed`` is False, ``reason`` is one of the
    audit reason strings: ``risk:halt``, ``risk:position_limit``,
    ``risk:daily_loss``, ``risk:exposure``, ``risk:market_hours``.
    The audit writer records the same string in
    ``order_attempt_audits.denied_reason``."""


class RiskAwareStrategy:
    """Strategy mixin that OVERRIDES the public submit cpdefs to enforce a
    node-side live halt before any order reaches Nautilus (F6 — PR 2 T2).

    Usage — the mixin MUST come FIRST in the base-class tuple so its gated
    overrides win the MRO::

        class MyStrategy(RiskAwareStrategy, Strategy):
            def on_bar(self, bar):
                order = self.order_factory.market(...)
                self.submit_order(order)   # transparently halt-gated

    The mixin overrides ``submit_order`` / ``submit_order_list`` /
    ``modify_order`` as Python ``def`` methods. ``Strategy.<method>`` is a
    Cython ``cpdef``, so a Python subclass override shadows it for the
    strategy's own Python-level calls (e.g. from ``on_bar``). Each override runs
    the halt gate, then calls the REAL Nautilus impl via the explicit unbound
    base call ``Strategy.submit_order(self, order, ...)`` — NEVER
    ``self.submit_order(...)`` (which would recurse infinitely). A strategy that
    calls ``self.submit_order(order)`` directly is therefore halt-gated with NO
    change to its body beyond declaring ``RiskAwareStrategy`` first.

    **Supported-API boundary (bounded guarantee).** The gate covers the
    supported public submit API (``submit_order`` / ``submit_order_list`` /
    ``modify_order``). A strategy that bypasses it via Nautilus internals
    (``self._manager.send_risk_command``, ``self._msgbus``, hand-built commands)
    is OUTSIDE the guaranteed surface — do NOT overclaim "all order paths". A
    static lint in ``strategies/`` rejects the obvious bypasses. A live strategy
    may also NOT override ``submit_order`` / ``submit_order_list`` /
    ``modify_order`` itself — the gate must be the EFFECTIVE implementation;
    ``on_post_build`` rejects any subclass that re-overrides them. Extend
    behavior in ``on_bar``, not by wrapping the submit methods.

    **Live-only arming (backtest-safe).** The same example strategies run in
    BACKTESTS too (loaded via ``ImportableStrategyConfig`` through the backtest
    runner, which has NO ``on_post_build`` hook and NO live halt latch). The
    halt branch is therefore armed ONLY in live: ``_halt_gate_armed`` defaults
    to ``False`` and is flipped to ``True`` by the live ``on_post_build`` wiring.
    When NOT armed, the halt branch is INERT (always allow) so backtests are
    never blocked. Fail-closed (``None`` / stale / ``True`` → block opening
    orders) applies ONLY when armed.

    **Halt gate semantics (per order).** Reduce-only / ``MARKET_EXIT`` orders are
    ALWAYS allowed (they only reduce risk — kill-all / drain flatten must work
    under a halt). The HALT-LATCH branch is LIVE-ONLY: it runs ONLY when
    ``_halt_gate_armed`` is ``True`` (set by the live wirer). For an opening order
    on an armed instance, the gate reads the synchronous live halt cache: ``None``
    (never fetched) OR older than :data:`HALT_CACHE_MAX_AGE_S` OR ``True`` → BLOCK
    (best-effort denial audit) and return without submitting. When NOT armed
    (backtest / non-live) the halt branch is SKIPPED entirely — a backtest has no
    live latch, so a ``None`` cache must never block it.

    **Risk suite is NOT gated by arming (FINDING 2 / P2).** The position /
    exposure / daily-loss / market-hours suite (``_run_risk_checks``) runs
    whenever ``_risk_limits`` is wired — REGARDLESS of ``_halt_gate_armed``. The
    ``_halt_gate_armed`` flag gates ONLY the live halt-latch branch above, NOT the
    general suite. ``_run_risk_checks`` self-skips (returns allowed) when
    ``_risk_limits is None``, so an unwired instance (the PR-2 default — per-
    account risk caps are PR-5 scope and have no live data path yet) submits a
    non-halted order without ``AttributeError``, while an unarmed instance WITH
    limits wired (a backtest with caps, or a legacy
    ``submit_order_with_risk_check`` caller) STILL enforces the suite. When armed,
    the halt branch runs BEFORE the suite (an active halt reports ``risk:halt``
    even if the order would also trip a limit). PR 5 wires ``_risk_limits`` + the
    cumulative-batch simulation for ``submit_order_list``.

    The mixin needs the following collaborators wired by the concrete strategy
    class (the live ``on_post_build`` wirer at deployment-start time):

    - ``self.portfolio`` — Nautilus ``Portfolio`` instance (provided by
      ``Strategy``; only used by the guarded PR-5 risk suite).
    - ``self._risk_limits`` — :class:`RiskLimits` for this deployment (PR 5;
      ``None`` in PR 2 → the suite is skipped).
    - ``self._audit`` — :class:`OrderAuditWriter` from Task 1.11 (best-effort;
      a missing/failed audit logs a warning and still BLOCKS — auditing the
      denial is never a precondition for blocking).
    - ``self._halt_cache`` — ``tuple[bool, float] | None`` fed by the background
      refresh task (``(value, monotonic_ts)``).
    - ``self._halt_gate_armed`` — set ``True`` only by the live wirer.
    - ``self._market_hours_check`` — optional callable ``(InstrumentId) -> bool``
      from Phase 4 Task 4.3 (only used by the guarded PR-5 risk suite).

    The mixin does NOT inherit from ``Strategy`` so it can be unit-tested without
    standing up a full Nautilus runtime. Concrete strategy classes inherit from
    BOTH this mixin and ``Strategy`` via standard Python multiple inheritance,
    with the mixin FIRST.
    """

    # ------------------------------------------------------------------
    # Required collaborator slots — populated by concrete subclass
    # ------------------------------------------------------------------
    portfolio: Any
    """Nautilus :class:`Portfolio` (or test stub)."""

    _risk_limits: RiskLimits | None = None
    """PR-5 per-deployment risk limits. ``None`` in PR 2 → the position /
    exposure / daily-loss suite is guarded/skipped (no live data path yet)."""

    _audit: OrderAuditWriter | None = None
    """Audit writer for denial records. Best-effort: a missing/failed audit
    still BLOCKS the order. ``None`` until the live wirer injects it."""

    _denial_context: DenialContext | None = None
    """Per-run identity (deployment_id / strategy_id / strategy_code_hash) the
    live wirer injects so a node-side halt block can write a COMPLETE ``denied``
    row (no ``submitted`` row exists for a pre-submit block). ``None`` → fall back
    to the UPDATE-only ``update_denied`` (best-effort; never fabricate identity)."""

    _halt_gate_armed: bool = False
    """LIVE-ONLY arming. ``False`` (class default) in backtests keeps the halt
    branch INERT. The live ``on_post_build`` wirer sets it ``True``."""

    _halt_cache: tuple[bool, float] | None = None
    """``(halted, monotonic_ts)`` written by the background refresh task.
    ``None`` = never fetched. See :data:`HALT_CACHE_MAX_AGE_S` for staleness."""

    _market_hours_check: Callable[[Any], bool] | None = None
    _refresh_halt_flag_fn: Callable[[], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # Public submit overrides — the gated path (F6, PR 2 T2)
    #
    # These shadow Nautilus's ``Strategy`` cpdefs for the strategy's own
    # Python-level calls. Each gates, then delegates to the REAL impl via the
    # EXPLICIT UNBOUND base call ``_base_submit_order(self, ...)`` — never
    # ``self.submit_order(...)`` (infinite recursion).
    # ------------------------------------------------------------------

    def submit_order(self, order: Any, *args: Any, **kwargs: Any) -> None:
        """Halt-gated override of ``Strategy.submit_order``.

        Reduce-only / ``MARKET_EXIT`` orders are always allowed. Otherwise, when
        the gate is armed (live) and halted/None/stale, the order is denied
        (best-effort audit) and NOT submitted. On allow, delegates to the real
        Nautilus impl.
        """
        if not self._gate_allows(order):
            return
        self._base_submit_order(order, *args, **kwargs)

    def submit_order_list(self, order_list: Any, *args: Any, **kwargs: Any) -> None:
        """Halt-gated override of ``Strategy.submit_order_list``.

        Atomic: if ANY non-reduce-only leg is halted, the WHOLE list is denied
        (no partial submit). A list whose every leg is reduce-only / flatten is
        always allowed (drain/kill-all flatten works under a halt).
        """
        legs = self._iter_order_list(order_list)
        if any(not self._gate_allows(leg) for leg in legs):
            return
        self._base_submit_order_list(order_list, *args, **kwargs)

    def modify_order(self, order: Any, *args: Any, **kwargs: Any) -> None:
        """Halt-gated override of ``Strategy.modify_order``.

        A modify that targets a reduce-only / flatten order is allowed; a modify
        that could increase exposure is treated as an opening order and is
        halt-gated.
        """
        if not self._gate_allows(order):
            return
        self._base_modify_order(order, *args, **kwargs)

    # ------------------------------------------------------------------
    # Unbound-base delegation helpers (single place the real cpdef is named).
    #
    # Resolved lazily so the mixin stays importable without ``nautilus_trader``
    # for unit tests. In production the concrete class is
    # ``class S(RiskAwareStrategy, Strategy)``; ``super()`` walks the MRO past
    # RiskAwareStrategy to Nautilus's ``Strategy`` cpdef — which is exactly the
    # real impl, and never re-enters this override.
    # ------------------------------------------------------------------

    def _base_submit_order(self, order: Any, *args: Any, **kwargs: Any) -> None:
        super().submit_order(order, *args, **kwargs)  # type: ignore[misc]

    def _base_submit_order_list(self, order_list: Any, *args: Any, **kwargs: Any) -> None:
        super().submit_order_list(order_list, *args, **kwargs)  # type: ignore[misc]

    def _base_modify_order(self, order: Any, *args: Any, **kwargs: Any) -> None:
        super().modify_order(order, *args, **kwargs)  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Retired helper (thin deprecated alias)
    # ------------------------------------------------------------------

    def submit_order_with_risk_check(self, order: Any) -> RiskCheckResult:
        """DEPRECATED thin alias — the gate now lives in the ``submit_order``
        override. Retained so any legacy caller keeps working. New code should
        call ``self.submit_order(order)`` directly.

        Returns the HONEST gate decision so a legacy caller can trust the
        result: ``allowed`` reflects whether the order was actually admitted AND
        ``reason`` reports the ACTUAL denial cause — ``risk:halt`` ONLY for a
        halt block, and the specific ``risk:position_limit`` / ``risk:daily_loss``
        / ``risk:exposure`` / ``risk:market_hours`` for a PR-5 risk-limit block
        (PR 2 F4 review P3). The SAME synchronous, deterministic gate
        (:meth:`_gate_decision`) is evaluated ONCE here — it records the denial
        audit on a block — and the order is submitted ONLY when admitted via the
        unbound base impl. Single gated path (no double-gating, no duplicate
        denial audit, no double-submit): the gate is evaluated exactly once and
        the submit is bound to that one decision, matching the override's own
        ``if not _gate_allows(order): return`` semantics.
        """
        decision = self._gate_decision(order)
        if not decision.allowed:
            return decision
        self._base_submit_order(order)
        return RiskCheckResult(allowed=True)

    # ------------------------------------------------------------------
    # Halt gate (synchronous, hot-path-safe)
    # ------------------------------------------------------------------

    def _gate_allows(self, order: Any) -> bool:
        """Return True iff *order* may be submitted right now.

        Thin bool wrapper over :meth:`_gate_decision` — the hot-path
        ``submit_order`` / ``submit_order_list`` / ``modify_order`` overrides
        only need the allow/deny bool. The structured decision (with the actual
        denial reason) is what :meth:`submit_order_with_risk_check` returns
        (PR 2 F4 review P3). Evaluating the gate ONCE here records the denial
        audit on a block.
        """
        return self._gate_decision(order).allowed

    def _gate_decision(self, order: Any) -> RiskCheckResult:
        """Evaluate the gate ONCE and return the structured decision carrying
        the ACTUAL denial reason.

        Order of evaluation (single pass, single audit, one HONEST reason):

        1. Reduce-only / ``MARKET_EXIT`` → ALWAYS allow (they only reduce risk —
           kill-all / drain flatten must work under a halt).
        2. **HALT-LATCH branch — LIVE-ONLY.** Runs ONLY when
           ``_halt_gate_armed`` (set ``True`` by the live ``on_post_build``
           wirer). The flag gates ONLY this branch: it reads the live Redis halt
           cache (``None`` / stale / ``True`` → BLOCK opening orders, reason
           ``risk:halt``). When NOT armed (backtest / non-live), this branch is
           INERT and is SKIPPED — a backtest has no live latch, so a ``None``
           cache must not block it.
        3. **General risk suite — ALWAYS runs when ``_risk_limits`` is wired,
           regardless of arming** (FINDING 2 / P2). ``_run_risk_checks`` already
           self-skips (returns allowed) when ``_risk_limits is None`` (the PR-2
           default). So an unarmed instance with limits wired (a backtest with
           caps, or a legacy ``submit_order_with_risk_check`` caller) STILL
           enforces the position / exposure / daily-loss / market-hours suite;
           an unarmed instance WITHOUT limits self-skips and allows the order.
           The reason carries the specific failing check
           (``risk:position_limit`` / ``risk:daily_loss`` / ``risk:exposure`` /
           ``risk:market_hours``).

        The denial audit is recorded here on a block — exactly once — so both the
        bool wrapper (:meth:`_gate_allows`) and the structured alias
        (:meth:`submit_order_with_risk_check`) share one evaluation, one audit,
        and one HONEST reason (no double-run of the checks). When armed, the halt
        branch runs BEFORE the suite, so an active halt reports ``risk:halt`` even
        if the order would also trip a risk limit.
        """
        if self._is_reduce_only_or_flatten(order):
            return RiskCheckResult(allowed=True)

        # HALT-LATCH branch — gated by ``_halt_gate_armed`` (live-only). Unarmed
        # (backtest / non-live) SKIPS this branch entirely; it never reaches the
        # halt cache, so a None/stale cache cannot block an unarmed order.
        if self._halt_gate_armed and self._halt_is_active():
            self._record_denial(order, reason="risk:halt")
            return RiskCheckResult(allowed=False, reason="risk:halt")

        # General risk suite — runs whenever ``_risk_limits`` is configured,
        # REGARDLESS of arming. ``_run_risk_checks`` self-skips when
        # ``_risk_limits is None`` (PR-2 default), so an unwired instance allows
        # the order without ``AttributeError``.
        result = self._run_risk_checks(order)
        if not result.allowed:
            reason = result.reason or "risk:unknown"
            self._record_denial(order, reason=reason)
            return RiskCheckResult(allowed=False, reason=reason)
        return RiskCheckResult(allowed=True)

    @staticmethod
    def _is_reduce_only_or_flatten(order: Any) -> bool:
        """Detect a reduce-only / flatten order — the SAME guard Nautilus uses
        internally (``strategy.pyx:861``). Such orders only reduce risk and are
        always allowed, even under a halt, so kill-all / drain flatten works."""
        if bool(getattr(order, "is_reduce_only", False)):
            return True
        tags = getattr(order, "tags", None) or []
        return "MARKET_EXIT" in tags

    def _halt_is_active(self) -> bool:
        """Read the synchronous live halt cache. ``None`` (never fetched) OR
        older than :data:`HALT_CACHE_MAX_AGE_S` OR value ``True`` → halted
        (fail-closed for new exposure). Only meaningful when armed."""
        cache = self._halt_cache
        if cache is None:
            return True
        halted, ts = cache
        if halted:
            return True
        return (time.monotonic() - ts) > HALT_CACHE_MAX_AGE_S

    @staticmethod
    def _iter_order_list(order_list: Any) -> list[Any]:
        """Extract the legs from an order-list so EVERY leg is halt-gated.

        Canonical Nautilus shape: an ``OrderList`` (has ``.orders``) — return
        its legs. Defensive (F2): if a bare ``list``/``tuple`` is passed (NOT the
        canonical Cython API), treat each element as a leg so an opening order
        can never reach the ungated ``super().submit_order_list`` delegation with
        empty legs. A truly-unrecognized non-None shape (no ``.orders``, not a
        list/tuple) returns ``[]`` — the subsequent delegation to Nautilus's
        Cython ``submit_order_list(OrderList)`` rejects it with TypeError (a safe
        error, NOT an ungated submit), so empty here is acceptable and is NOT a
        bypass. A legitimately-empty ``OrderList`` (``.orders=[]``) also returns
        ``[]`` and is correctly allowed to delegate (nothing to gate)."""
        orders = getattr(order_list, "orders", None)
        if orders is not None:
            return list(orders)
        if isinstance(order_list, (list, tuple)):
            return list(order_list)
        return []

    async def refresh_halt_cache(self, read_halt: Callable[[], Awaitable[bool]]) -> None:
        """Refresh :attr:`_halt_cache` from *read_halt* (an async reader of the
        fleet+account halt latches). Stamps ``monotonic()`` so the sync gate can
        detect staleness. On a reader error the cache is left untouched (stale →
        the gate fails closed)."""
        value = bool(await read_halt())
        self._halt_cache = (value, time.monotonic())

    # ------------------------------------------------------------------
    # Individual checks (each returns a RiskCheckResult)
    # ------------------------------------------------------------------

    def _run_risk_checks(self, order: Any) -> RiskCheckResult:
        """Execute the position/exposure/daily-loss/market-hours suite.

        GUARDED: when ``_risk_limits`` is unset (PR-2 default) the suite is
        SKIPPED entirely (returns allowed) — those checks need a live risk-limit
        data path that PR 5 adds. The HALT gate (the PR-2 mandatory check) runs
        BEFORE this in :meth:`_gate_allows`, not here.
        """
        if self._risk_limits is None:
            # PR-2: per-account risk caps not wired — skip the suite.
            return RiskCheckResult(allowed=True)

        if not self._check_position_limit(order):
            return RiskCheckResult(allowed=False, reason="risk:position_limit")

        if not self._check_daily_loss_limit(order):
            return RiskCheckResult(allowed=False, reason="risk:daily_loss")

        if not self._check_exposure_limit(order):
            return RiskCheckResult(allowed=False, reason="risk:exposure")

        if not self._check_market_hours(order):
            return RiskCheckResult(allowed=False, reason="risk:market_hours")

        return RiskCheckResult(allowed=True)

    def _check_position_limit(self, order: Any) -> bool:
        """Per-instrument net position cap. Uses
        ``portfolio.net_position(instrument_id)`` (singular —
        per-instrument). The projected position is current +
        signed order quantity; rejected if abs(projected)
        exceeds the limit.
        """
        assert self._risk_limits is not None  # noqa: S101 — guaranteed by _run_risk_checks guard
        instrument_id = order.instrument_id
        # Nautilus returns Decimal-like value or None
        current = self.portfolio.net_position(instrument_id) or Decimal("0")
        signed_qty = self._signed_quantity(order)
        projected = Decimal(str(current)) + signed_qty
        return abs(projected) <= self._risk_limits.max_position_per_instrument

    def _check_daily_loss_limit(self, order: Any) -> bool:
        """Per-venue total PnL across all currencies (in USD).
        Uses ``portfolio.total_pnls(venue, target_currency=USD)``
        — PLURAL — which returns ``dict[Currency, Money]``.

        Codex v3 P1 regression: v3 wrongly called the singular
        ``total_pnl(venue)`` which expects an ``InstrumentId``,
        not a ``Venue``, so the call returned ``None`` and
        the daily-loss check was a silent no-op.
        """
        venue = order.instrument_id.venue
        usd = self._usd_currency()
        if usd is None:
            # Nothing to compare against; let the order through
            return True
        venue_pnls = self.portfolio.total_pnls(venue, target_currency=usd)
        if not venue_pnls:
            # No PnL data yet (cold start) — let the order through
            return True
        return self._within_daily_loss_limit(venue_pnls)

    def _check_exposure_limit(self, order: Any) -> bool:
        """Per-venue net exposure (USD aggregate). Uses
        ``portfolio.net_exposures(venue, target_currency=USD)``
        — PLURAL — which returns ``dict[Currency, Money]``.
        Adds the new order's notional to the projected total.
        """
        venue = order.instrument_id.venue
        usd = self._usd_currency()
        if usd is None:
            return True
        venue_exposures = self.portfolio.net_exposures(venue, target_currency=usd)
        if not venue_exposures:
            return True
        return self._within_exposure_limit(venue_exposures, order)

    def _check_market_hours(self, order: Any) -> bool:
        """Defer to the optional market-hours check. Phase 4
        Task 4.3 wires this from the ``instrument_cache.trading_hours``
        column written in Phase 2. Until then the default
        ``None`` callable means "always allow"."""
        if self._market_hours_check is None:
            return True
        try:
            return bool(self._market_hours_check(order.instrument_id))
        except Exception:  # noqa: BLE001
            log.exception("risk_market_hours_check_failed")
            # Fail-closed: an exception in the check is treated
            # as "outside hours" so we don't accidentally
            # submit during a maintenance window.
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _within_daily_loss_limit(self, pnls: dict[Any, Any]) -> bool:
        """Sum the per-currency PnL values (already converted
        to USD by Nautilus because we passed
        ``target_currency=USD``) and compare against the
        configured daily loss limit. The total is negative
        when the venue is at a loss; we reject if the loss
        exceeds the absolute limit.
        """
        assert self._risk_limits is not None  # noqa: S101 — guaranteed by _run_risk_checks guard
        total = Decimal("0")
        for money in pnls.values():
            total += self._money_to_decimal(money)
        # ``daily_loss_limit_usd`` is positive; reject if total
        # PnL is more negative than the limit.
        return total >= -self._risk_limits.daily_loss_limit_usd

    def _within_exposure_limit(self, exposures: dict[Any, Any], order: Any) -> bool:
        """Sum venue-level net exposures (USD-converted) and
        add the order's notional. Reject if the projected
        total exceeds the limit. Notional is computed as
        ``quantity * price`` for limit orders, or
        ``quantity * 0`` for market orders (the engine doesn't
        know the fill price yet — we let market orders through
        the exposure check).
        """
        assert self._risk_limits is not None  # noqa: S101 — guaranteed by _run_risk_checks guard
        current_total = Decimal("0")
        for money in exposures.values():
            current_total += self._money_to_decimal(money)
        order_notional = self._order_notional(order)
        projected = current_total + order_notional
        return projected <= self._risk_limits.max_notional_exposure_usd

    def _signed_quantity(self, order: Any) -> Decimal:
        """Return the order quantity signed by side: BUY
        positive, SELL negative. The mixin works with both
        Nautilus enum values and string sides for test-friendliness."""
        side = getattr(order, "side", None)
        side_str = str(getattr(side, "name", side) or "").upper()
        qty = Decimal(str(getattr(order, "quantity", 0)))
        if side_str == "SELL":
            return -qty
        return qty

    def _order_notional(self, order: Any) -> Decimal:
        """Project the dollar exposure of one order. For
        limit/STOP orders we have a price. For market orders
        the price is None — we return zero so the exposure
        check doesn't reject every market order.
        """
        price = getattr(order, "price", None)
        if price is None:
            return Decimal("0")
        return Decimal(str(order.quantity)) * Decimal(str(price))

    @staticmethod
    def _money_to_decimal(money: Any) -> Decimal:
        """Coerce a Nautilus ``Money`` (or test stub) to a
        ``Decimal``. ``Money.as_decimal()`` is the canonical
        accessor; we fall back to ``str()`` for test stubs
        that don't have it.
        """
        if hasattr(money, "as_decimal"):
            return Decimal(str(money.as_decimal()))
        return Decimal(str(money))

    @staticmethod
    def _usd_currency() -> Any | None:
        """Return Nautilus's ``Currency`` instance for USD or
        ``None`` if Nautilus isn't importable (e.g. unit
        tests don't need a real Currency object). The mixin
        only uses this for the ``target_currency`` kwarg to
        Nautilus's portfolio APIs."""
        try:
            from nautilus_trader.model.currencies import USD
        except ImportError:
            return None
        return USD

    def _record_denial(self, order: Any, *, reason: str) -> None:
        """Synchronously fire-and-forget the audit denial.
        The ``OrderAuditWriter`` write is async, so we schedule it
        without awaiting — the strategy is on Nautilus's hot path and
        can't block on a database round-trip.

        Durable denial (PR 2 T2 audit-completeness fix)
        -----------------------------------------------
        A node-side halt block returns BEFORE the order reaches
        Nautilus, so NO ``OrderSubmitted`` event fires and the
        engine-level audit hook never inserts a ``submitted`` row.
        When the per-run identity is wired (``_denial_context`` set by
        the live ``on_post_build`` hook), we record a COMPLETE
        ``denied`` row via :meth:`OrderAuditWriter.write_denied` —
        an idempotent UPSERT on ``client_order_id`` that INSERTs when
        no row exists (the halt-block case) and flips an existing
        ``submitted`` row to ``denied`` otherwise. This satisfies
        ``nautilus.md`` #10 (every real-money order attempt is
        durably auditable).

        When ``_denial_context`` is NOT wired we fall back to the
        UPDATE-only :meth:`OrderAuditWriter.update_denied` (which
        matches nothing for a pre-submit block) rather than
        fabricating placeholder identity into a real-money audit row
        — a wrong strategy_id/deployment_id is worse than a
        logged-only denial.

        BEST-EFFORT (PR 2 T2): the denial audit is NEVER a precondition
        for blocking — the order has already been blocked by
        ``_gate_allows`` before this runs. A missing (``None``) or
        raising ``_audit`` MUST log and continue; it MUST NOT raise (no
        crash) and MUST NOT admit the order.

        We use ``asyncio.get_running_loop()`` (NOT the deprecated
        ``get_event_loop``) so that running this outside of an event
        loop fails fast in tests rather than silently creating a new
        loop. When the strategy runs inside Nautilus's live engine the
        loop is always present.
        """
        log.warning(
            "risk_check_denied",
            extra={
                "client_order_id": str(getattr(order, "client_order_id", "")),
                "instrument_id": str(getattr(order, "instrument_id", "")),
                "reason": reason,
            },
        )
        if self._audit is None:
            # No audit writer wired — block stands (already enforced); the denial
            # simply goes unrecorded beyond the warning above.
            log.warning("risk_audit_writer_missing_denial_unrecorded", extra={"reason": reason})
            return
        try:
            import asyncio

            client_order_id = str(order.client_order_id)
            coro = self._build_denial_coro(order, client_order_id=client_order_id, reason=reason)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop — most commonly in unit
                # tests that drive the mixin synchronously.
                # Call the audit method and close the
                # returned coroutine so Python doesn't warn
                # about a never-awaited coroutine on GC.
                if asyncio.iscoroutine(coro):
                    coro.close()
                return
            loop.create_task(coro)
        except Exception:  # noqa: BLE001
            log.exception("risk_audit_denial_write_failed")

    def _build_denial_coro(self, order: Any, *, client_order_id: str, reason: str) -> Any:
        """Build the audit coroutine for a denial.

        With ``_denial_context`` wired (live), build complete
        :class:`OrderDeniedFacts` from the per-run identity + the
        blocked order's own fields and call
        :meth:`OrderAuditWriter.write_denied` (idempotent UPSERT — a
        durable ``denied`` row even when no ``submitted`` row exists).
        Without it, fall back to the UPDATE-only ``update_denied``.

        Order fields are read defensively (``getattr`` with safe
        fallbacks) — this is the hot path and a missing attribute on a
        test stub or an exotic order shape must never crash the gate.
        """
        assert self._audit is not None  # noqa: S101 — caller guarantees (checked in _record_denial)
        ctx = self._denial_context
        if ctx is None:
            # No per-run identity wired — best-effort UPDATE-only path.
            # Will match nothing for a pre-submit block, but never
            # fabricates placeholder identity into a real-money row.
            return self._audit.update_denied(client_order_id, reason=reason)

        from datetime import UTC, datetime

        from msai.services.nautilus.audit_hook import OrderDeniedFacts

        side = getattr(order, "side", None)
        side_str = str(getattr(side, "name", side) or "")
        order_type = getattr(order, "order_type", None)
        order_type_str = str(getattr(order_type, "name", order_type) or "")
        raw_qty = getattr(order, "quantity", 0)
        try:
            quantity = Decimal(str(raw_qty))
        except (ArithmeticError, ValueError, TypeError):
            quantity = Decimal("0")
        raw_price = getattr(order, "price", None)
        price: Decimal | None
        if raw_price is None:
            price = None
        else:
            try:
                price = Decimal(str(raw_price))
            except (ArithmeticError, ValueError, TypeError):
                price = None

        facts = OrderDeniedFacts(
            client_order_id=client_order_id,
            strategy_id=ctx.strategy_id,
            strategy_code_hash=ctx.strategy_code_hash,
            instrument_id=str(getattr(order, "instrument_id", "")),
            side=side_str,
            quantity=quantity,
            price=price,
            order_type=order_type_str,
            ts_attempted=datetime.now(UTC),
            reason=reason,
            deployment_id=ctx.deployment_id,
            is_live=True,
            strategy_git_sha=ctx.strategy_git_sha,
        )
        return self._audit.write_denied(facts)
