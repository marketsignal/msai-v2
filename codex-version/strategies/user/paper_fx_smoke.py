from __future__ import annotations

from decimal import Decimal

from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events.order import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy


class PaperFXSmokeConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal = Decimal("25000")
    exit_after_bars: int = 1


class PaperFXSmokeStrategy(Strategy):
    """Tiny IB paper-forex smoke strategy.

    It buys once on the first bar and flattens on the next bar after the fill.
    This is only for broker-connected paper-trading certification.
    """

    def __init__(self, config: PaperFXSmokeConfig) -> None:
        super().__init__(config=config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type
        self.trade_size = Quantity.from_str(str(config.trade_size))
        self.exit_after_bars = max(1, int(config.exit_after_bars))
        self._entry_submitted = False
        self._entry_filled = False
        self._exit_submitted = False

    def on_start(self) -> None:
        if not self._entry_submitted:
            self._submit_market_order(OrderSide.BUY)
            self._entry_submitted = True

    def on_order_filled(self, event: OrderFilled) -> None:
        if event.instrument_id != self.instrument_id:
            return

        if not self._entry_filled and event.order_side == OrderSide.BUY:
            self._entry_filled = True
            if not self._exit_submitted:
                self.close_all_positions(self.instrument_id)
                self._exit_submitted = True

    def on_stop(self) -> None:
        self.cancel_all_orders(self.instrument_id)
        self.close_all_positions(self.instrument_id)

    def _submit_market_order(self, side: OrderSide) -> None:
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.trade_size,
            time_in_force=TimeInForce.DAY,
        )
        self.submit_order(order)
