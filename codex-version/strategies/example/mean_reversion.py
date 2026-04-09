from __future__ import annotations

from collections import deque
from decimal import Decimal
from statistics import fmean, pstdev

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy


class MeanReversionZScoreConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    lookback: int = 20
    entry_zscore: float = 1.5
    exit_zscore: float = 0.25
    trade_size: Decimal = Decimal("1")
    max_hold_bars: int = 30
    allow_shorting: bool = True


class MeanReversionZScoreStrategy(Strategy):
    """Intraday z-score mean reversion on minute bars.

    The strategy buys statistically weak prices and sells statistically strong
    prices back toward the rolling mean. It is designed to be simple and
    parameterized so we can use it as the first research/sweep candidate.
    """

    def __init__(self, config: MeanReversionZScoreConfig) -> None:
        super().__init__(config=config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type
        self.lookback = max(2, int(config.lookback))
        self.entry_zscore = float(config.entry_zscore)
        self.exit_zscore = max(0.0, float(config.exit_zscore))
        self.trade_size = Quantity.from_str(str(config.trade_size))
        self.max_hold_bars = max(1, int(config.max_hold_bars))
        self.allow_shorting = bool(config.allow_shorting)

        self._closes: deque[float] = deque(maxlen=self.lookback)
        self._bars_in_position = 0

    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        close_price = float(bar.close)
        self._closes.append(close_price)

        if len(self._closes) < self.lookback:
            return

        stddev = pstdev(self._closes)
        if stddev <= 0:
            return

        mean_price = fmean(self._closes)
        zscore = (close_price - mean_price) / stddev

        if self.portfolio.is_flat(self.instrument_id):
            self._bars_in_position = 0
            if zscore <= -self.entry_zscore:
                self._submit_market_order(OrderSide.BUY)
            elif self.allow_shorting and zscore >= self.entry_zscore:
                self._submit_market_order(OrderSide.SELL)
            return

        self._bars_in_position += 1
        if self._should_exit(zscore):
            self.close_all_positions(self.instrument_id)
            self._bars_in_position = 0

    def on_stop(self) -> None:
        self.cancel_all_orders(self.instrument_id)
        self.close_all_positions(self.instrument_id)

    def _should_exit(self, zscore: float) -> bool:
        if self._bars_in_position >= self.max_hold_bars:
            return True
        if self.portfolio.is_net_long(self.instrument_id):
            return zscore >= -self.exit_zscore
        if self.portfolio.is_net_short(self.instrument_id):
            return zscore <= self.exit_zscore
        return False

    def _submit_market_order(self, side: OrderSide) -> None:
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.trade_size,
        )
        self.submit_order(order)
