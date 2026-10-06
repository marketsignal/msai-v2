"""Deterministic smoke strategy (Phase 1 task 1.15).

Submits exactly ONE tiny market order on the first bar received, then
sits idle. Used by the Phase 1 E2E harness (task 1.16) to prove the
order path end-to-end — EMA cross may not fire during a short E2E
window (Codex plan finding #8), but this strategy always fires on
the first bar.

Design (plan v9 decision #11):

- Inherits directly from :class:`nautilus_trader.trading.strategy.Strategy`
  (not an MSAI wrapper) — per the "use Nautilus API, never reinvent"
  rule. Every method called is a real Nautilus primitive.
- **No custom ``on_stop`` override.** ``manage_stop=True`` on the
  config tells Nautilus to cancel all open orders and flatten
  positions automatically when the strategy is stopped
  (``nautilus_trader/trading/strategy.pyx`` — the base class
  handles the flatten-on-stop loop).
- ``order_id_tag`` is injected from the deployment_slug at config
  build time (Task 1.5 / 1.10) so every ``client_order_id`` Nautilus
  mints on this strategy is prefix-stable across restarts. Task 1.11's
  audit hook uses that prefix to correlate orders to a deployment.
"""

from __future__ import annotations

# The V2 candidate supports this strategy in research only; live is refused.
from typing import Any, Self

# The user schema helper resolves constructor annotations at runtime.
from nautilus_trader.model import Bar, BarType, InstrumentId, OrderSide, Quantity  # noqa: TC002
from nautilus_trader.trading import Strategy, StrategyConfig

from msai.services.nautilus.risk import RiskAwareStrategy


class SmokeMarketOrderConfig(StrategyConfig):
    """Native config with required instrument/bar identity and stop cleanup.

    Native base fields are initialized by __new__. Keep manage_stop=True there
    as well as in the declared user defaults; order_id_tag remains native
    plumbing and uses None unless explicitly supplied by an owning runtime.
    """

    def __new__(cls, **kwargs: Any) -> Self:
        # Native base fields are initialized before Python __init__ runs.
        kwargs.setdefault("manage_stop", True)
        return super().__new__(cls, **kwargs)

    def __init__(
        self,
        *,
        instrument_id: InstrumentId,
        bar_type: BarType,
        manage_stop: bool = True,
        order_id_tag: str | None = None,
    ) -> None:
        super().__init__()
        self.instrument_id = (
            InstrumentId.from_str(instrument_id)
            if isinstance(instrument_id, str)
            else instrument_id
        )
        self.bar_type = (
            BarType.from_str(bar_type) if isinstance(bar_type, str) else bar_type
        )


# RC6 modify_order uses ClientOrderId; the retained V1 live mixin uses order.
# This mixin is unarmed in research; RC6 live is refused and we do not modify orders.
class SmokeMarketOrderStrategy(RiskAwareStrategy, Strategy):  # type: ignore[misc]
    """Submits exactly ONE market-order buy on the first bar received.

    After the single order is submitted the strategy sits idle forever
    — subsequent bars are ignored. This determinism is what makes it
    useful for the Phase 1 E2E harness: the harness knows exactly
    how many orders to expect (one) and can assert on the audit
    table accordingly.

    ``RiskAwareStrategy`` is FIRST in the base-class tuple (PR 2 T2 / F6) so its
    halt-gated ``submit_order`` override wins the MRO. The body still calls
    ``self.submit_order(order)`` directly — it is now transparently node-side
    halt-gated in LIVE (and inert in backtests, where the gate is never armed).

    Position cleanup at stop time is handled by ``manage_stop=True`` —
    Nautilus's base ``Strategy`` cancels open orders and flattens any
    open positions when the engine stops this strategy. We deliberately
    do NOT override ``on_stop`` here (gotcha #13 — custom on_stop
    pre-v3 was a bug because it raced the engine's own shutdown).
    """

    def __init__(self, config: SmokeMarketOrderConfig) -> None:
        super().__init__(config=config)
        self.instrument_id: InstrumentId = config.instrument_id
        self.bar_type: BarType = config.bar_type
        self._order_submitted = False

    def on_start(self) -> None:
        """Subscribe to the configured bar stream.

        No indicators — this strategy doesn't care about price, only
        about the fact that a bar was delivered (which means the
        data path is alive). ``subscribe_bars`` is the real Nautilus
        method on the ``Strategy`` base.
        """
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:  # noqa: ARG002 — bar arg required by Nautilus contract
        """Submit exactly one market BUY on the very first bar, then
        noop forever after. Guarded by ``_order_submitted`` so a
        slow order-status round-trip or a replay doesn't produce a
        second order."""
        if self._order_submitted:
            return

        order = self._build_market_order()
        self.submit_order(order)
        self._order_submitted = True

    # Extracted into a Python-level method (rather than inlined in
    # ``on_bar``) so unit tests can subclass and override it without
    # touching Nautilus's Cython slot attributes. Production always
    # uses the real ``order_factory.market`` path.
    def _build_market_order(self) -> Any:
        return self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str("1"),
        )
