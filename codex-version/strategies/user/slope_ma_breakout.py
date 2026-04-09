from __future__ import annotations

from collections import deque
from decimal import Decimal
from statistics import fmean

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy


class SlopeMovingAverageBreakoutConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    entry_lookback: int = 55
    exit_lookback: int = 20
    moving_average_period: int = 34
    slope_lookback: int = 8
    atr_period: int = 14
    breakout_buffer_atr: float = 0.15
    trailing_stop_atr: float = 1.5
    require_price_above_ma: bool = True
    min_entry_slope_bps_per_bar: float = 0.5
    flat_exit_slope_bps_per_bar: float = 0.0
    use_slope_exit: bool = False
    trade_size: Decimal = Decimal("1")
    allow_shorting: bool = False


class SlopeMovingAverageBreakoutStrategy(Strategy):
    """Trend-qualified breakout strategy filtered by moving-average slope.

    The core idea is still the same: a breakout only matters when the
    underlying trend is already moving with enough speed. This revision makes
    that idea more robust in three ways:

    - it requires price to already sit on the correct side of the moving average
    - it measures trend slope using a smoothed regression-style estimate rather
      than a single two-point difference
    - it uses ATR-based breakout confirmation and trailing exits so volatility
      is handled explicitly instead of assuming every breakout level means the
      same thing

    Long entries require both:
    - price closing above a recent close-based breakout channel by an ATR buffer
    - moving-average slope steep enough to confirm trend strength
    - price already trading above the moving average

    Short entries mirror the same logic when shorting is enabled.
    """

    def __init__(self, config: SlopeMovingAverageBreakoutConfig) -> None:
        super().__init__(config=config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type
        self.entry_lookback = max(2, int(config.entry_lookback))
        self.exit_lookback = max(2, int(config.exit_lookback))
        self.moving_average_period = max(2, int(config.moving_average_period))
        self.slope_lookback = max(1, int(config.slope_lookback))
        self.atr_period = max(2, int(config.atr_period))
        self.breakout_buffer_atr = max(0.0, float(config.breakout_buffer_atr))
        self.trailing_stop_atr = max(0.0, float(config.trailing_stop_atr))
        self.require_price_above_ma = bool(config.require_price_above_ma)
        self.min_entry_slope_bps_per_bar = float(config.min_entry_slope_bps_per_bar)
        self.flat_exit_slope_bps_per_bar = float(config.flat_exit_slope_bps_per_bar)
        self.use_slope_exit = bool(config.use_slope_exit)
        self.trade_size = Quantity.from_str(str(config.trade_size))
        self.allow_shorting = bool(config.allow_shorting)

        history_window = max(
            self.entry_lookback,
            self.exit_lookback,
            self.atr_period + 1,
            self.moving_average_period + self.slope_lookback + 1,
        )
        self._highs: deque[float] = deque(maxlen=history_window)
        self._lows: deque[float] = deque(maxlen=history_window)
        self._closes: deque[float] = deque(maxlen=history_window)
        self._moving_average_values: deque[float] = deque(maxlen=self.slope_lookback + 2)
        self._highest_close_since_entry: float | None = None
        self._lowest_close_since_entry: float | None = None

    def on_start(self) -> None:
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))
        self._closes.append(float(bar.close))

        if len(self._closes) < self.moving_average_period:
            return

        current_ma = fmean(list(self._closes)[-self.moving_average_period :])
        self._moving_average_values.append(current_ma)
        slope_bps_per_bar = self._moving_average_slope_bps_per_bar()
        if slope_bps_per_bar is None:
            return

        atr_value = self._average_true_range()
        if atr_value is None or atr_value <= 0:
            return
        if len(self._highs) <= self.entry_lookback:
            return

        previous_closes = list(self._closes)[:-1]
        previous_lows = list(self._lows)[:-1]
        previous_highs = list(self._highs)[:-1]
        close_price = self._closes[-1]

        entry_high = max(previous_closes[-self.entry_lookback :])
        entry_low = min(previous_closes[-self.entry_lookback :])
        exit_high = max(previous_highs[-self.exit_lookback :])
        exit_low = min(previous_lows[-self.exit_lookback :])
        breakout_buffer = self.breakout_buffer_atr * atr_value

        if self.portfolio.is_flat(self.instrument_id):
            self._highest_close_since_entry = None
            self._lowest_close_since_entry = None
            long_trend_ok = slope_bps_per_bar >= self.min_entry_slope_bps_per_bar and (
                not self.require_price_above_ma or close_price >= current_ma
            )
            short_trend_ok = slope_bps_per_bar <= -self.min_entry_slope_bps_per_bar and (
                not self.require_price_above_ma or close_price <= current_ma
            )

            if close_price >= entry_high + breakout_buffer and long_trend_ok:
                self._submit_market_order(OrderSide.BUY)
                self._highest_close_since_entry = close_price
            elif (
                self.allow_shorting
                and close_price <= entry_low - breakout_buffer
                and short_trend_ok
            ):
                self._submit_market_order(OrderSide.SELL)
                self._lowest_close_since_entry = close_price
            return

        if self.portfolio.is_net_long(self.instrument_id):
            self._highest_close_since_entry = max(self._highest_close_since_entry or close_price, close_price)
            trailing_stop = self._highest_close_since_entry - (self.trailing_stop_atr * atr_value)
            if (
                close_price <= max(exit_low, trailing_stop)
                or self._should_exit_long_on_slope(slope_bps_per_bar)
            ):
                self.close_all_positions(self.instrument_id)
        elif self.portfolio.is_net_short(self.instrument_id):
            self._lowest_close_since_entry = min(self._lowest_close_since_entry or close_price, close_price)
            trailing_stop = self._lowest_close_since_entry + (self.trailing_stop_atr * atr_value)
            if (
                close_price >= min(exit_high, trailing_stop)
                or self._should_exit_short_on_slope(slope_bps_per_bar)
            ):
                self.close_all_positions(self.instrument_id)

    def on_stop(self) -> None:
        self.cancel_all_orders(self.instrument_id)
        self.close_all_positions(self.instrument_id)

    def _moving_average_slope_bps_per_bar(self) -> float | None:
        if len(self._moving_average_values) <= self.slope_lookback:
            return None

        values = list(self._moving_average_values)[-self.slope_lookback - 1 :]
        slope = _linear_regression_slope(values)
        current_ma = values[-1]
        if current_ma == 0:
            return None

        return (slope / current_ma) * 10_000.0

    def _average_true_range(self) -> float | None:
        if len(self._closes) <= self.atr_period:
            return None

        highs = list(self._highs)
        lows = list(self._lows)
        closes = list(self._closes)
        true_ranges = [
            _true_range(
                high=highs[index],
                low=lows[index],
                previous_close=closes[index - 1],
            )
            for index in range(1, len(closes))
        ]
        if len(true_ranges) < self.atr_period:
            return None
        return fmean(true_ranges[-self.atr_period :])

    def _should_exit_long_on_slope(self, slope_bps_per_bar: float) -> bool:
        return self.use_slope_exit and slope_bps_per_bar <= self.flat_exit_slope_bps_per_bar

    def _should_exit_short_on_slope(self, slope_bps_per_bar: float) -> bool:
        return self.use_slope_exit and slope_bps_per_bar >= -self.flat_exit_slope_bps_per_bar

    def _submit_market_order(self, side: OrderSide) -> None:
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.trade_size,
        )
        self.submit_order(order)


def _linear_regression_slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0

    count = len(values)
    x_mean = (count - 1) / 2.0
    y_mean = fmean(values)
    numerator = 0.0
    denominator = 0.0
    for index, value in enumerate(values):
        x_delta = float(index) - x_mean
        numerator += x_delta * (value - y_mean)
        denominator += x_delta * x_delta
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _true_range(*, high: float, low: float, previous_close: float) -> float:
    return max(
        high - low,
        abs(high - previous_close),
        abs(low - previous_close),
    )
