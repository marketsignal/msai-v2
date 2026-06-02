"""Cycling buy/sell TEST strategy — live data-flow + execution demo.

Every ``cycle_bars`` bars (default 2 → 2 minutes on a 1-MINUTE bar stream),
the strategy toggles between opening a tiny long and flattening it: it BUYs
``quantity`` when its own cycle is flat, then SELLs the same ``quantity`` one
cycle later, and repeats indefinitely.

Purpose (operator/test only — this has NO trading edge):
  - Prove the LIVE path is flowing end-to-end: Databento (EQUS.MINI) real-time
    bars → strategy ``on_bar`` → IB execution → fill. Each trade requires a
    fresh bar to have actually arrived, so continuous trading == live data is
    flowing.
  - Exercise per-account, multi-strategy live trading (one instance per symbol).

SAFETY — why we track our OWN cycle state instead of ``portfolio.net_position``:
  The account may already hold an EXTERNAL position in the instrument (e.g. a
  pre-existing SPY holding surfaced by startup reconciliation). Keying the
  BUY/SELL decision off ``net_position`` would make the strategy SELL into that
  external holding. Instead we track ``self._is_long`` (this strategy's own
  cycle only) and always SELL exactly the ``quantity`` we previously BOUGHT, so
  the account merely oscillates by ``quantity`` on top of whatever it already
  holds — the external position is never reduced.

Mirrors :mod:`smoke_market_order` for structure: inherits the real Nautilus
``Strategy`` with ``RiskAwareStrategy`` FIRST in the MRO so ``submit_order`` is
node-side halt-gated in live (inert in backtests), and relies on
``manage_stop=True`` for flatten-on-stop (no custom ``on_stop`` — gotcha #13).
"""

from __future__ import annotations

# Nautilus msgspec configs resolve field annotations at runtime via inspect,
# so ``InstrumentId``/``BarType`` must be importable at module load.
from nautilus_trader.model.data import Bar, BarType  # noqa: TC002
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled  # noqa: TC002
from nautilus_trader.model.identifiers import InstrumentId  # noqa: TC002
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from msai.services.nautilus.risk import RiskAwareStrategy


class CycleBuySellConfig(StrategyConfig, frozen=True, kw_only=True):
    """Config for :class:`CycleBuySellStrategy`.

    ``cycle_bars`` is the number of bars between each BUY/SELL toggle (2 bars
    of a 1-MINUTE stream == a 2-minute cycle). ``quantity`` is the share count
    per leg (keep tiny for real-money tests). ``order_id_tag`` is inherited and
    injected by the live config builder (see ``smoke_market_order`` for why we
    don't redeclare it).
    """

    instrument_id: InstrumentId
    bar_type: BarType
    cycle_bars: int = 2
    quantity: int = 1
    manage_stop: bool = True


class CycleBuySellStrategy(RiskAwareStrategy, Strategy):
    """BUY then SELL ``quantity`` every ``cycle_bars`` bars, forever."""

    def __init__(self, config: CycleBuySellConfig) -> None:
        super().__init__(config=config)
        self.instrument_id: InstrumentId = config.instrument_id
        self.bar_type: BarType = config.bar_type
        self.cycle_bars: int = config.cycle_bars
        self.quantity: int = config.quantity
        self._bar_count: int = 0
        # This strategy's OWN cycle state — NOT the account net position.
        self._is_long: bool = False

    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)
        self.log.info(
            f"CycleBuySell START {self.instrument_id} "
            f"cycle_bars={self.cycle_bars} qty={self.quantity}"
        )

    def on_bar(self, bar: Bar) -> None:
        self._bar_count += 1
        # Only act on every Nth bar — a fresh bar having arrived is the proof
        # that live data is flowing.
        if self._bar_count % self.cycle_bars != 0:
            return

        side = OrderSide.SELL if self._is_long else OrderSide.BUY
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=Quantity.from_int(self.quantity),
        )
        self.log.info(
            f"CycleBuySell bar#{self._bar_count} {self.instrument_id} "
            f"-> {side.name} {self.quantity} (close={bar.close})"
        )
        # _is_long is advanced in on_order_filled, NOT here. If the node-side
        # halt gate (or a stale halt cache) BLOCKS this submit, the order never
        # fills, _is_long stays put, and the next cycle RETRIES the same side —
        # so a blocked BUY can never leave the strategy "long" and SELL a
        # position it never opened (which could reduce an external holding).
        # State follows real fills, not optimistic intent.
        self.submit_order(order)

    def on_order_filled(self, event: OrderFilled) -> None:
        """Advance the cycle state only on an ACTUAL fill — the only proof the
        leg happened. Guarded to this strategy's own instrument."""
        if event.instrument_id != self.instrument_id:
            return
        self._is_long = event.order_side == OrderSide.BUY
