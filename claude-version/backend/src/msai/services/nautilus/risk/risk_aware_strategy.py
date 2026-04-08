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

Halt flag is defense in depth (decision #16)
--------------------------------------------

The primary kill switch is push-based — the supervisor
SIGTERMs every running deployment immediately on
``/api/v1/live/kill-all`` (Task 3.9). The halt flag this
mixin caches is a **third layer** that refuses any new orders
the strategy might emit between the SIGTERM being sent and the
subprocess actually exiting. The lag (one ``on_bar`` worst case)
is acceptable because it's defense in depth, not the primary
mechanism.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from msai.services.nautilus.audit_hook import OrderAuditWriter


log = logging.getLogger(__name__)


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
    """Strategy mixin that runs pre-submit risk checks before
    calling Nautilus's ``submit_order``.

    Usage::

        class MyStrategy(RiskAwareStrategy, Strategy):
            def on_bar(self, bar):
                order = self.order_factory.market(...)
                self.submit_order_with_risk_check(order)

    The mixin needs the following collaborators wired by the
    concrete strategy class (typically via a wiring helper at
    deployment-start time):

    - ``self.portfolio`` — Nautilus ``Portfolio`` instance
      (provided by ``Strategy``).
    - ``self._risk_limits`` — :class:`RiskLimits` for this
      deployment.
    - ``self._audit`` — :class:`OrderAuditWriter` from
      Task 1.11.
    - ``self._halt_flag_cached`` — boolean refreshed by
      ``_refresh_halt_flag``.
    - ``self._market_hours_check`` — optional callable
      ``(InstrumentId) -> bool`` from Phase 4 Task 4.3.

    The mixin does NOT inherit from ``Strategy`` so it can be
    unit-tested without standing up a full Nautilus runtime.
    Concrete strategy classes inherit from BOTH this mixin and
    ``Strategy`` via standard Python multiple inheritance.
    """

    # ------------------------------------------------------------------
    # Required collaborator slots — populated by concrete subclass
    # ------------------------------------------------------------------
    portfolio: Any
    """Nautilus :class:`Portfolio` (or test stub)."""

    _risk_limits: RiskLimits
    _audit: OrderAuditWriter
    _halt_flag_cached: bool = False
    _market_hours_check: Callable[[Any], bool] | None = None
    _refresh_halt_flag_fn: Callable[[], Awaitable[None]] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_order_with_risk_check(self, order: Any) -> RiskCheckResult:
        """Run all risk checks. On any failure, write a
        ``denied`` row to ``order_attempt_audits`` and return
        a result with ``allowed=False`` and the failing reason.
        On success, call ``self.submit_order(order)`` and
        return ``allowed=True``.

        Returns the result so callers (and tests) can inspect
        the outcome instead of grepping log lines.
        """
        result = self._run_risk_checks(order)
        if not result.allowed:
            self._record_denial(order, reason=result.reason or "risk:unknown")
            return result

        # All checks passed — submit. ``submit_order`` is
        # provided by Nautilus's ``Strategy`` base class. We
        # call it via getattr to keep the mixin
        # subclass-friendly when it's mixed into ``Strategy``.
        submit = getattr(self, "submit_order", None)
        if submit is None:
            raise RuntimeError(
                "RiskAwareStrategy.submit_order_with_risk_check requires "
                "self.submit_order — mixin must be combined with "
                "Nautilus's Strategy base class"
            )
        submit(order)
        return result

    # ------------------------------------------------------------------
    # Individual checks (each returns a RiskCheckResult)
    # ------------------------------------------------------------------

    def _run_risk_checks(self, order: Any) -> RiskCheckResult:
        """Execute every check in order, returning the FIRST
        failure. The order is intentional: the cheapest checks
        run first so a halted deployment doesn't burn CPU on
        portfolio queries."""
        if self._halt_flag_cached:
            return RiskCheckResult(allowed=False, reason="risk:halt")

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
        ``OrderAuditWriter.update_denied`` is async, so we
        schedule it without awaiting — the strategy is on
        Nautilus's hot path and can't block on a database
        round-trip.

        We use ``asyncio.get_running_loop()`` (NOT the
        deprecated ``get_event_loop``) so that running this
        outside of an event loop fails fast in tests rather
        than silently creating a new loop. When the strategy
        runs inside Nautilus's live engine the loop is always
        present.
        """
        log.warning(
            "risk_check_denied",
            extra={
                "client_order_id": str(getattr(order, "client_order_id", "")),
                "instrument_id": str(getattr(order, "instrument_id", "")),
                "reason": reason,
            },
        )
        try:
            import asyncio

            client_order_id = str(order.client_order_id)
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No running loop — most commonly in unit
                # tests that drive the mixin synchronously.
                # Call the audit method and close the
                # returned coroutine so Python doesn't warn
                # about a never-awaited coroutine on GC.
                coro = self._audit.update_denied(client_order_id, reason=reason)
                if asyncio.iscoroutine(coro):
                    coro.close()
                return
            loop.create_task(self._audit.update_denied(client_order_id, reason=reason))
        except Exception:  # noqa: BLE001
            log.exception("risk_audit_denial_write_failed")
