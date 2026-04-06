"""EMA Cross Strategy -- buys when fast EMA crosses above slow EMA.

A portable crossover strategy that works with the MSAI backtesting framework.
For NautilusTrader deployment, a separate adapter wraps this logic.
"""

from collections import deque


class EMACrossConfig:
    """Configuration for the EMA crossover strategy."""

    def __init__(
        self,
        fast_period: int = 10,
        slow_period: int = 20,
        trade_size: float = 100.0,
        instrument: str = "AAPL",
    ) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.trade_size = trade_size
        self.instrument = instrument


class EMACrossStrategy:
    """Simple EMA crossover strategy.

    Buys when fast EMA crosses above slow EMA,
    sells when fast EMA crosses below slow EMA.

    Portable implementation that works with the MSAI backtesting framework.
    For NautilusTrader deployment, a separate adapter wraps this logic.
    """

    def __init__(
        self,
        fast_period: int = 10,
        slow_period: int = 20,
        trade_size: float = 100.0,
        instrument: str = "AAPL",
        config: "EMACrossConfig | None" = None,
    ) -> None:
        if config is not None:
            self.fast_period = config.fast_period
            self.slow_period = config.slow_period
            self.trade_size = config.trade_size
            self.instrument = config.instrument
        else:
            self.fast_period = fast_period
            self.slow_period = slow_period
            self.trade_size = trade_size
            self.instrument = instrument

        self.fast_ema: deque[float] = deque(maxlen=2)
        self.slow_ema: deque[float] = deque(maxlen=2)
        self.position: int = 0  # 0=flat, 1=long, -1=short
        self.trades: list = []

    def on_bar(self, bar: dict) -> "dict | None":
        """Process a bar and return a trade signal dict or None.

        Args:
            bar: Dictionary with at least a ``close`` key containing the price.

        Returns:
            A trade signal dict with ``side``, ``price``, ``quantity`` keys,
            or ``None`` if no signal is generated.
        """
        price = float(bar["close"])
        self._update_ema(price)

        if len(self.fast_ema) < 2:
            return None

        # Crossover detection: fast crosses above slow -> BUY
        if self.fast_ema[-1] > self.slow_ema[-1] and self.fast_ema[-2] <= self.slow_ema[-2]:
            if self.position <= 0:
                self.position = 1
                signal = {
                    "side": "BUY",
                    "price": price,
                    "quantity": self.trade_size,
                }
                self.trades.append(signal)
                return signal

        # Crossover detection: fast crosses below slow -> SELL
        elif self.fast_ema[-1] < self.slow_ema[-1] and self.fast_ema[-2] >= self.slow_ema[-2]:
            if self.position >= 0:
                self.position = -1
                signal = {
                    "side": "SELL",
                    "price": price,
                    "quantity": self.trade_size,
                }
                self.trades.append(signal)
                return signal

        return None

    def _update_ema(self, price: float) -> None:
        """Update EMA values with a new price observation.

        Uses the standard exponential moving average formula:
        ``EMA_new = price * k + EMA_prev * (1 - k)`` where ``k = 2 / (period + 1)``.
        """
        for period, ema_list in [
            (self.fast_period, self.fast_ema),
            (self.slow_period, self.slow_ema),
        ]:
            if not ema_list:
                ema_list.append(price)
            else:
                k = 2.0 / (period + 1)
                ema_list.append(price * k + ema_list[-1] * (1.0 - k))
