"""Second cycling buy/sell TEST strategy — identical behavior to
:mod:`cycle_buy_sell`, distinct class so it registers as a SEPARATE
``strategy_id``.

The portfolio model allows only ONE member per ``strategy_id`` per revision, so
trading two symbols on one account requires two distinct strategies. This is
the "B" sibling: same single-instrument 2-minute BUY/SELL cycle, used for the
second symbol on each account (e.g. AAPL on LVP, MSFT on HVP) while
``cycle_buy_sell`` handles the first (SPY on LVP, QQQ on HVP).

See :mod:`cycle_buy_sell` for the full design + safety rationale (internal
cycle-state tracking, never sells into external holdings, RiskAwareStrategy
halt-gate MRO, manage_stop flatten-on-stop).
"""

from __future__ import annotations

from nautilus_trader.model.data import Bar, BarType  # noqa: TC002
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.events import OrderFilled  # noqa: TC002
from nautilus_trader.model.identifiers import InstrumentId  # noqa: TC002
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy

from msai.services.nautilus.risk import RiskAwareStrategy


class CycleBuySellBConfig(StrategyConfig, frozen=True, kw_only=True):
    """Config for :class:`CycleBuySellBStrategy` (mirror of ``CycleBuySellConfig``)."""

    instrument_id: InstrumentId
    bar_type: BarType
    cycle_bars: int = 2
    quantity: int = 1
    manage_stop: bool = True


class CycleBuySellBStrategy(RiskAwareStrategy, Strategy):
    """BUY then SELL ``quantity`` every ``cycle_bars`` bars, forever (sibling B)."""

    def __init__(self, config: CycleBuySellBConfig) -> None:
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
            f"CycleBuySellB START {self.instrument_id} "
            f"cycle_bars={self.cycle_bars} qty={self.quantity}"
        )

    def on_bar(self, bar: Bar) -> None:
        self._bar_count += 1
        if self._bar_count % self.cycle_bars != 0:
            return

        side = OrderSide.SELL if self._is_long else OrderSide.BUY
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=Quantity.from_int(self.quantity),
        )
        self.log.info(
            f"CycleBuySellB bar#{self._bar_count} {self.instrument_id} "
            f"-> {side.name} {self.quantity} (close={bar.close})"
        )
        # _is_long advances in on_order_filled, NOT here — a halt-blocked submit
        # never fills, so the next cycle retries the same side and the strategy
        # never SELLs a position it didn't open (cannot reduce external holdings).
        self.submit_order(order)

    def on_order_filled(self, event: OrderFilled) -> None:
        """Advance the cycle state only on an ACTUAL fill. Guarded to this
        strategy's own instrument."""
        if event.instrument_id != self.instrument_id:
            return
        self._is_long = event.order_side == OrderSide.BUY
