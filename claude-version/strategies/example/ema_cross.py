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

        # Phase 4 task 4.5: restart-continuity state. The
        # bar-ts idempotency key is persisted via on_save /
        # on_load so a restart picks up exactly where the
        # previous run left off without re-emitting a
        # duplicate decision on the first bar after restart.
        #
        # Why we do NOT persist a "last position state":
        # Nautilus's LiveExecEngineConfig.reconciliation=True
        # (Phase 1 task 1.5) restores portfolio state from
        # the broker on every restart. Persisting our own
        # copy would be redundant AND would create a
        # consistency hazard if the persisted state diverged
        # from the post-reconciliation portfolio state. The
        # reconciliation lookback (1440 minutes) is the
        # source of truth for "where was I when the
        # subprocess died" — Codex batch 10 P1 iter 2 fix.
        self._last_decision_bar_ts_ns: int | None = None
        """``ts_event`` of the last bar that produced a trade
        decision. ``on_bar`` rejects any bar with
        ``ts_event <= self._last_decision_bar_ts_ns`` as
        already-seen — Nautilus replays buffered bars on
        restart and we must not duplicate the trade."""

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

        # Restart-continuity idempotency (Phase 4 task 4.5):
        # Nautilus may replay buffered bars on restart. The
        # ``last_decision_bar_ts`` we restored from on_load is
        # the ``ts_event`` of the last bar that DID produce a
        # decision. Skip any bar at or before that ts so we
        # don't double-trade on the same data. Codex batch
        # 10 P2 fix.
        bar_ts = getattr(bar, "ts_event", None)
        if (
            bar_ts is not None
            and self._last_decision_bar_ts_ns is not None
            and bar_ts <= self._last_decision_bar_ts_ns
        ):
            return

        decision_made = False

        # Golden cross: fast above slow -> be long.
        if self.fast_ema.value > self.slow_ema.value:
            if self.portfolio.is_flat(self.instrument_id):
                self._submit_market_order(OrderSide.BUY)
                decision_made = True
            elif self.portfolio.is_net_short(self.instrument_id):
                self.close_all_positions(self.instrument_id)
                self._submit_market_order(OrderSide.BUY)
                decision_made = True

        # Death cross: fast below slow -> flatten any long position.
        elif self.fast_ema.value < self.slow_ema.value and self.portfolio.is_net_long(
            self.instrument_id
        ):
            self.close_all_positions(self.instrument_id)
            decision_made = True

        # Record the bar ts so on the NEXT restart, on_load
        # restores it and the post-restart on_bar skips any
        # buffered re-delivery of this same bar.
        if decision_made and bar_ts is not None:
            self._last_decision_bar_ts_ns = int(bar_ts)

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

    # ------------------------------------------------------------------
    # State persistence (Phase 4 task 4.5)
    # ------------------------------------------------------------------

    def on_save(self) -> dict[str, bytes]:
        """Persist EMA indicator state on shutdown.

        Called by Nautilus's kernel when ``save_state=True``
        on ``TradingNodeConfig``. The dict is written to the
        cache backend (Redis in production, in-memory in
        tests). On restart, ``on_load`` reads the same dict
        back and restores the strategy state so the next bar
        sees a fully-warmed indicator instead of a cold
        first-bar default.

        Schema (versioned via the ``version`` key so a future
        format change doesn't crash on a stale on-disk
        state):

        - ``version`` — schema marker (currently ``b"3"``)
        - ``fast_ema_value`` / ``slow_ema_value`` — current
          indicator readings
        - ``fast_ema_count`` / ``slow_ema_count`` — input
          count tracked by Nautilus's ``MovingAverage`` base
          class. Persisting this is what lets ``on_load``
          restore an UNINITIALIZED indicator as
          uninitialized.
        - ``last_decision_bar_ts`` — ``ts_event`` of the last
          bar that produced a trade decision; the
          idempotency key on the next ``on_bar`` call

        Codex batch 10 P1 iter 2 fix: dropped
        ``last_position_state`` because it was dead state
        (no read site in ``on_bar``). Portfolio recovery on
        restart is handled by Nautilus's
        ``LiveExecEngineConfig.reconciliation=True`` (Phase
        1 task 1.5), which is the actual source of truth.
        """
        return {
            "version": b"3",
            "fast_ema_value": str(self.fast_ema.value).encode(),
            "slow_ema_value": str(self.slow_ema.value).encode(),
            "fast_ema_count": str(self.fast_ema.count).encode(),
            "slow_ema_count": str(self.slow_ema.count).encode(),
            "last_decision_bar_ts": str(self._last_decision_bar_ts_ns or 0).encode(),
        }

    def on_load(self, state: dict[str, bytes]) -> None:
        """Restore strategy state on startup.

        Called by Nautilus's kernel when ``load_state=True``
        on ``TradingNodeConfig``. ``state`` is the dict
        ``on_save`` last wrote to the cache backend. An
        empty dict OR a wrong-version dict means cold start
        — the strategy starts with default indicator state
        and the FIRST bar will warm the indicators normally.

        Codex batch 10 P1 fix: replays the saved value
        ``min(saved_count, period)`` times so the indicator's
        ``count`` and ``initialized`` flags match the
        pre-restart state EXACTLY. A pre-restart strategy
        with ``count=1`` will report ``initialized=False``
        after restart, just like before — preventing a
        false-positive trade signal on the first
        post-restart bar.
        """
        if not state or state.get("version") != b"3":
            return  # Cold start

        try:
            fast = float(state["fast_ema_value"].decode())
            slow = float(state["slow_ema_value"].decode())
            fast_count = int(state["fast_ema_count"].decode())
            slow_count = int(state["slow_ema_count"].decode())
            last_ts = int(state["last_decision_bar_ts"].decode())
        except (KeyError, ValueError, UnicodeDecodeError):
            # Malformed state — fall back to cold start
            # rather than crashing the strategy on a stale
            # cache entry from an old format.
            return

        # Replay the saved value exactly ``count`` times so
        # the indicator's ``count`` and ``initialized`` end
        # state matches the pre-restart state. We do NOT cap
        # at ``period`` (Codex batch 10 P3 fix) — that would
        # round count=11 down to count=10 and lose state
        # fidelity. Nautilus's ``MovingAverage._update_raw``
        # flips ``initialized`` exactly when
        # ``count >= period``; any extra replays past period
        # are no-ops on the value (the EMA formula with
        # constant input is stable) but they DO advance
        # count, which is what we want.
        for _ in range(fast_count):
            self.fast_ema.update_raw(fast)
        for _ in range(slow_count):
            self.slow_ema.update_raw(slow)
        self._last_decision_bar_ts_ns = last_ts or None
