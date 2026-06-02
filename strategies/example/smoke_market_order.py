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

# Nautilus msgspec configs resolve field annotations at runtime via
# inspect, so ``InstrumentId``/``BarType`` must be importable at
# module load, not only under ``TYPE_CHECKING``.
from nautilus_trader.model.data import Bar, BarType  # noqa: TC002
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId  # noqa: TC002
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from msai.services.nautilus.risk import RiskAwareStrategy


class SmokeMarketOrderConfig(StrategyConfig, frozen=True, kw_only=True):
    """Config for :class:`SmokeMarketOrderStrategy`.

    ``instrument_id`` + ``bar_type`` are required. ``manage_stop`` and
    ``order_id_tag`` are inherited from the base
    :class:`nautilus_trader.trading.config.StrategyConfig` and injected at
    runtime by ``build_live_trading_node_config`` (Task 1.10) with real
    values; backtests rely on the base class default ``order_id_tag=None``.

    Why we DON'T redeclare ``order_id_tag`` with a default of ``""``:
    Nautilus's strategy constructor builds ``StrategyId`` as
    ``f"{component_id}-{config.order_id_tag}"`` (``trading/strategy.pyx:149``).
    An empty-string ``order_id_tag`` produces ``"SmokeMarketOrderStrategy-"``
    which the Rust ``StrategyId`` validator rejects with
    ``Condition failed: 'value' tag part (after '-') cannot be empty`` —
    the subprocess panics before it can write a result pickle, so the
    parent BacktestRunner sees an empty file and raises ``EOFError: Ran
    out of input``. Leaving ``order_id_tag`` inherited (default ``None``)
    produces ``"SmokeMarketOrderStrategy-None"`` which the validator
    accepts, and production live deployments always inject a real value
    via the live config builder.

    ``kw_only=True`` is required because ``StrategyConfig`` (the base)
    has fields with defaults, and msgspec refuses required positional
    fields following optional ones. ``kw_only`` sidesteps the
    ordering constraint entirely.
    """

    instrument_id: InstrumentId
    bar_type: BarType
    manage_stop: bool = True


class SmokeMarketOrderStrategy(RiskAwareStrategy, Strategy):
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
    def _build_market_order(self):  # type: ignore[no-untyped-def]
        return self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=Quantity.from_str("1"),
        )
