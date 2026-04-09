from __future__ import annotations

from collections import deque
from decimal import Decimal

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy


class DonchianBreakoutConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    entry_lookback: int = 20
    exit_lookback: int = 10
    trade_size: Decimal = Decimal("1")
    allow_shorting: bool = True


class DonchianBreakoutStrategy(Strategy):
    """Simple Donchian breakout strategy for trend research.

    This gives us a trend-following baseline alongside the mean-reversion
    strategy so sweeps and walk-forward tests compare distinct behaviors.
    """

    def __init__(self, config: DonchianBreakoutConfig) -> None:
        super().__init__(config=config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type
        self.entry_lookback = max(2, int(config.entry_lookback))
        self.exit_lookback = max(2, int(config.exit_lookback))
        self.trade_size = Quantity.from_str(str(config.trade_size))
        self.allow_shorting = bool(config.allow_shorting)

        maxlen = max(self.entry_lookback, self.exit_lookback) + 1
        self._highs: deque[float] = deque(maxlen=maxlen)
        self._lows: deque[float] = deque(maxlen=maxlen)
        self._closes: deque[float] = deque(maxlen=maxlen)

    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))
        self._closes.append(float(bar.close))

        if len(self._closes) <= self.entry_lookback:
            return

        previous_highs = list(self._highs)[:-1]
        previous_lows = list(self._lows)[:-1]
        close_price = self._closes[-1]

        entry_high = max(previous_highs[-self.entry_lookback:])
        entry_low = min(previous_lows[-self.entry_lookback:])
        exit_high = max(previous_highs[-self.exit_lookback:])
        exit_low = min(previous_lows[-self.exit_lookback:])

        if self.portfolio.is_flat(self.instrument_id):
            if close_price >= entry_high:
                self._submit_market_order(OrderSide.BUY)
            elif self.allow_shorting and close_price <= entry_low:
                self._submit_market_order(OrderSide.SELL)
            return

        if self.portfolio.is_net_long(self.instrument_id) and close_price <= exit_low:
            self.close_all_positions(self.instrument_id)
        elif self.portfolio.is_net_short(self.instrument_id) and close_price >= exit_high:
            self.close_all_positions(self.instrument_id)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.instrument_id)
        self.close_all_positions(self.instrument_id)

    def _submit_market_order(self, side: OrderSide) -> None:
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.trade_size,
        )
        self.submit_order(order)
