from __future__ import annotations

from nautilus_trader.model.data import Bar
from nautilus_trader.trading.strategy import Strategy

from strategies.example.config import EMACrossConfig


class EMACrossStrategy(Strategy):
    """NautilusTrader EMA crossover strategy scaffold."""

    def __init__(self, config: EMACrossConfig) -> None:
        super().__init__(config=config)
        self.config = config

    def on_start(self) -> None:
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        _ = bar

    def on_stop(self) -> None:
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)
