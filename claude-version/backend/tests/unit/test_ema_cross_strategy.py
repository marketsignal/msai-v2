"""Unit tests for the EMA Cross strategy."""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Use a relative import path consistent with the strategy registry tests.
# The strategy lives at strategies/example/ema_cross.py relative to the backend root.
from pathlib import Path

from msai.services.strategy_registry import load_strategy_class

_STRATEGY_PATH = Path(__file__).resolve().parents[2] / "strategies" / "example" / "ema_cross.py"


def _load_strategy_class() -> type:
    """Load the EMACrossStrategy class from the strategies directory."""
    return load_strategy_class(_STRATEGY_PATH, "EMACrossStrategy")


def _make_bar(close: float) -> dict[str, float]:
    """Create a minimal bar dict with only a close price."""
    return {"close": close}


def _generate_rising_then_falling_bars(
    n_rise: int = 30, n_fall: int = 30, base: float = 100.0
) -> list[dict[str, float]]:
    """Generate bars that rise steadily then fall, ensuring EMA crossovers."""
    bars: list[dict[str, float]] = []
    # Rising phase: price goes up
    for i in range(n_rise):
        bars.append(_make_bar(base + i * 2.0))
    # Falling phase: price drops sharply
    peak = base + (n_rise - 1) * 2.0
    for i in range(n_fall):
        bars.append(_make_bar(peak - i * 3.0))
    return bars


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEMACrossStrategyInstantiation:
    """Tests for EMACrossStrategy instantiation."""

    def test_strategy_instantiates_with_defaults(self) -> None:
        """EMACrossStrategy() creates an instance with default config."""
        # Arrange
        cls = _load_strategy_class()

        # Act
        strategy = cls()

        # Assert
        assert strategy.fast_period == 10
        assert strategy.slow_period == 20
        assert strategy.position == 0
        assert strategy.trades == []

    def test_strategy_instantiates_with_custom_params(self) -> None:
        """EMACrossStrategy(fast_period=5, slow_period=15) works correctly."""
        # Arrange
        cls = _load_strategy_class()

        # Act
        strategy = cls(fast_period=5, slow_period=15)

        # Assert
        assert strategy.fast_period == 5
        assert strategy.slow_period == 15

    def test_strategy_instantiates_with_config_object(self) -> None:
        """EMACrossStrategy with EMACrossConfig object works."""
        # Arrange
        cls = _load_strategy_class()
        config_cls = load_strategy_class(_STRATEGY_PATH, "EMACrossConfig")
        config = config_cls(fast_period=3, slow_period=8, trade_size=50.0)

        # Act
        strategy = cls(config=config)

        # Assert
        assert strategy.fast_period == 3
        assert strategy.slow_period == 8
        assert strategy.trade_size == 50.0


class TestEMACrossOnBar:
    """Tests for EMACrossStrategy.on_bar signal generation."""

    def test_on_bar_returns_none_with_insufficient_data(self) -> None:
        """First bar should return None (need at least 2 EMA values)."""
        # Arrange
        cls = _load_strategy_class()
        strategy = cls(fast_period=3, slow_period=5)

        # Act
        signal = strategy.on_bar(_make_bar(100.0))

        # Assert
        assert signal is None

    def test_on_bar_generates_buy_signal(self) -> None:
        """Feed rising prices to trigger a BUY crossover signal."""
        # Arrange
        cls = _load_strategy_class()
        strategy = cls(fast_period=3, slow_period=10, trade_size=100.0)

        # Start with flat prices then rise sharply to force fast EMA above slow EMA
        signals: list[dict[str, object]] = []

        # Flat period: both EMAs converge
        for _ in range(15):
            result = strategy.on_bar(_make_bar(100.0))
            if result is not None:
                signals.append(result)

        # Sharp rise: fast EMA will cross above slow EMA
        for i in range(20):
            result = strategy.on_bar(_make_bar(100.0 + (i + 1) * 5.0))
            if result is not None:
                signals.append(result)

        # Assert
        buy_signals = [s for s in signals if s["side"] == "BUY"]
        assert len(buy_signals) >= 1
        assert buy_signals[0]["side"] == "BUY"
        assert buy_signals[0]["quantity"] == 100.0

    def test_on_bar_generates_sell_signal(self) -> None:
        """Feed rising then falling prices to trigger BUY then SELL."""
        # Arrange
        cls = _load_strategy_class()
        strategy = cls(fast_period=3, slow_period=10, trade_size=50.0)

        bars = _generate_rising_then_falling_bars(n_rise=30, n_fall=30)
        signals: list[dict[str, object]] = []

        # Act
        for bar in bars:
            result = strategy.on_bar(bar)
            if result is not None:
                signals.append(result)

        # Assert
        sell_signals = [s for s in signals if s["side"] == "SELL"]
        assert len(sell_signals) >= 1
        assert sell_signals[0]["side"] == "SELL"
        assert sell_signals[0]["quantity"] == 50.0

    def test_on_bar_no_signal_when_flat(self) -> None:
        """Flat prices should not generate crossover signals."""
        # Arrange
        cls = _load_strategy_class()
        strategy = cls(fast_period=5, slow_period=10)

        signals: list[dict[str, object]] = []

        # Act: feed constant prices
        for _ in range(50):
            result = strategy.on_bar(_make_bar(100.0))
            if result is not None:
                signals.append(result)

        # Assert
        assert len(signals) == 0

    def test_on_bar_tracks_trades(self) -> None:
        """Strategy.trades list accumulates all generated signals."""
        # Arrange
        cls = _load_strategy_class()
        strategy = cls(fast_period=3, slow_period=10)

        bars = _generate_rising_then_falling_bars(n_rise=30, n_fall=30)

        # Act
        for bar in bars:
            strategy.on_bar(bar)

        # Assert
        assert len(strategy.trades) >= 1
        for trade in strategy.trades:
            assert "side" in trade
            assert "price" in trade
            assert "quantity" in trade
