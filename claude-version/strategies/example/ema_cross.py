"""EMA crossover strategy built on NautilusTrader.

A minimal "golden cross / death cross" reference strategy used by MSAI's
backtests and live deployments.  It registers two
``ExponentialMovingAverage`` indicators (a fast one and a slow one), feeds
them with every incoming :class:`Bar`, and flips a single position long
whenever the fast EMA crosses above the slow EMA, closing the position
when it crosses back below.

Design notes
------------
* This is a **real** NautilusTrader :class:`Strategy` subclass -- it is not
  a portable Python loop with a Nautilus adapter.  The backtest engine
  instantiates it directly inside the spawned subprocess via
  ``ImportableStrategyConfig``.
* Position management is deliberately simple: one symbol, one position at a
  time, market orders only.  This mirrors the way most educational EMA
  examples ship and is enough to verify the end-to-end backtest pipeline
  produces non-zero trades.
* The matching configuration model lives in
  :mod:`strategies.example.config` and is imported here by Nautilus via the
  ``ImportableStrategyConfig.config_path`` resolved by
  :mod:`msai.services.nautilus.strategy_loader`.
"""

from __future__ import annotations

from nautilus_trader.indicators import ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from strategies.example.config import EMACrossConfig


class EMACrossStrategy(Strategy):
    """Buy-on-golden-cross / sell-on-death-cross EMA strategy.

    The strategy subscribes to a single bar type and keeps two EMAs
    updated from those bars.  Trading rules:

    * When both EMAs are initialised and ``fast > slow`` and the portfolio
      is flat on the instrument -> submit a market **BUY** for the
      configured trade size.  If we are currently short, flatten first
      then go long.
    * When ``fast < slow`` and we are net long -> close the position.

    The intentionally coarse logic makes it trivial to reason about how
    many trades a given historical window should produce, which is
    important for smoke-testing the full Nautilus backtest pipeline.
    """

    def __init__(self, config: EMACrossConfig) -> None:
        """Initialise EMAs and remember the instrument / bar spec.

        Args:
            config: Frozen :class:`EMACrossConfig` containing the
                instrument ID, bar type, EMA periods and trade size.
        """
        super().__init__(config=config)
        self.instrument_id: InstrumentId = config.instrument_id
        self.bar_type: BarType = config.bar_type
        self.trade_size: Quantity = Quantity.from_str(str(config.trade_size))

        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)

    def on_start(self) -> None:
        """Subscribe to bars and wire indicators into the bar stream.

        Called once by Nautilus after the strategy is registered with the
        engine.  We only need bar data here -- no ticks, no order book.
        """
        self.register_indicator_for_bars(self.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.bar_type, self.slow_ema)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        """Evaluate the crossover on every incoming bar.

        Args:
            bar: The current :class:`Bar` emitted by the engine.  Only
                used for the implicit indicator update -- we read EMA
                values directly since they're already updated by the
                time this callback fires.
        """
        # Wait until both indicators have enough history to emit values.
        if not self.fast_ema.initialized or not self.slow_ema.initialized:
            return

        # Golden cross: fast above slow -> be long.
        if self.fast_ema.value > self.slow_ema.value:
            if self.portfolio.is_flat(self.instrument_id):
                self._submit_market_order(OrderSide.BUY)
                return
            if self.portfolio.is_net_short(self.instrument_id):
                self.close_all_positions(self.instrument_id)
                self._submit_market_order(OrderSide.BUY)
                return

        # Death cross: fast below slow -> flatten any long position.
        elif self.fast_ema.value < self.slow_ema.value:
            if self.portfolio.is_net_long(self.instrument_id):
                self.close_all_positions(self.instrument_id)

    def on_stop(self) -> None:
        """Flatten positions and cancel working orders on shutdown.

        Called by Nautilus when the engine tears down the strategy --
        either at the end of a backtest run or when a live deployment is
        stopped.  Leaving orders or open positions here would leak across
        runs in the shared engine state.
        """
        self.cancel_all_orders(self.instrument_id)
        self.close_all_positions(self.instrument_id)

    def _submit_market_order(self, side: OrderSide) -> None:
        """Build and submit a market order for the configured trade size.

        Extracted into a helper so the ``on_bar`` logic stays readable and
        so we only have one place to tweak if we ever swap market orders
        for limit orders or add slippage controls.

        Args:
            side: :class:`OrderSide.BUY` or :class:`OrderSide.SELL`.
        """
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=self.trade_size,
        )
        self.submit_order(order)
