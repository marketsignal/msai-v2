from __future__ import annotations

from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from strategies.example.config import EMACrossConfig


class EMACrossStrategy(Strategy):
    """NautilusTrader EMA crossover strategy.

    Buys when fast EMA crosses above slow EMA (golden cross),
    sells/closes when fast EMA crosses below slow EMA (death cross).
    """

    def __init__(self, config: EMACrossConfig) -> None:
        super().__init__(config=config)
        self.instrument_id: InstrumentId = config.instrument_id
        self.bar_type: BarType = config.bar_type
        self.trade_size = Quantity.from_str(str(config.trade_size))

        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

    def on_start(self) -> None:
        self.register_indicator_for_bars(self.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.bar_type, self.slow_ema)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        if not self.fast_ema.initialized or not self.slow_ema.initialized:
            return

        # Golden cross: fast crosses above slow → buy
        if self.fast_ema.value > self.slow_ema.value:
            if self.portfolio.is_flat(self.instrument_id):
                self.buy(self.instrument_id, self.trade_size)
            elif self.portfolio.is_net_short(self.instrument_id):
                self.close_all_positions(self.instrument_id)
                self.buy(self.instrument_id, self.trade_size)

        # Death cross: fast crosses below slow → sell
        elif self.fast_ema.value < self.slow_ema.value:
            if self.portfolio.is_net_long(self.instrument_id):
                self.close_all_positions(self.instrument_id)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.instrument_id)
        self.close_all_positions(self.instrument_id)

    def buy(self, instrument_id: InstrumentId, quantity: Quantity) -> None:
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=OrderSide.BUY,
            quantity=quantity,
        )
        self.submit_order(order)
