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

# The V2 candidate supports this strategy in research only; live is refused.
from typing import Annotated, Any, Self

# The user schema helper resolves constructor annotations at runtime.
from nautilus_trader.model import (  # noqa: TC002
    Bar,
    BarType,
    InstrumentId,
    OrderFilled,
    OrderSide,
    Quantity,
)
from nautilus_trader.trading import Strategy, StrategyConfig
from pydantic import Field

from msai.services.nautilus.risk import RiskAwareStrategy


class CycleBuySellConfig(StrategyConfig):
    """Config for :class:`CycleBuySellStrategy`.

    ``cycle_bars`` is the number of bars between each BUY/SELL toggle (2 bars
    of a 1-MINUTE stream == a 2-minute cycle). ``quantity`` is the share count
    per leg (keep tiny for real-money tests). ``order_id_tag`` is inherited and
    injected by the live config builder (see ``smoke_market_order`` for why we
    don't redeclare it).
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
        cycle_bars: Annotated[int, Field(gt=0, strict=True)] = 2,
        quantity: Annotated[int, Field(gt=0, strict=True)] = 1,
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
        self.cycle_bars = cycle_bars
        self.quantity = quantity


# RC6 modify_order uses ClientOrderId; the retained V1 live mixin uses order.
# This mixin is unarmed in research; RC6 live is refused and we do not modify orders.
class CycleBuySellStrategy(RiskAwareStrategy, Strategy):  # type: ignore[misc]
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
