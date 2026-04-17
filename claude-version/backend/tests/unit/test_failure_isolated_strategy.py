"""Unit tests for FailureIsolatedStrategy mixin.

The mixin wraps Nautilus strategy event handlers so that one
buggy strategy in a multi-strategy TradingNode degrades
gracefully instead of crashing the entire node.  Tests use a
lightweight stub instead of a real Nautilus Strategy to keep
them fast and dependency-free.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from msai.services.nautilus.failure_isolated_strategy import (
    FailureIsolatedStrategy,
    StrategyDegradedError,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubLog:
    """Mimics Nautilus's ``Logger`` (strategy.log) used inside
    the wrapper.  Records calls for assertion."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)


class HealthyStrategy(FailureIsolatedStrategy):
    """Strategy whose on_bar runs without error."""

    def __init__(self) -> None:
        self.log = _StubLog()
        self.bars_received: list[Any] = []
        self.ticks_received: list[Any] = []

    def on_bar(self, bar: Any) -> None:
        self.bars_received.append(bar)

    def on_quote_tick(self, tick: Any) -> None:
        self.ticks_received.append(tick)


class BuggyBarStrategy(FailureIsolatedStrategy):
    """Strategy whose on_bar always raises."""

    def __init__(self) -> None:
        self.log = _StubLog()

    def on_bar(self, bar: Any) -> None:
        raise ValueError("bad bar logic")


class BuggyTickStrategy(FailureIsolatedStrategy):
    """Strategy whose on_quote_tick raises."""

    def __init__(self) -> None:
        self.log = _StubLog()
        self.bars_received: list[Any] = []

    def on_bar(self, bar: Any) -> None:
        self.bars_received.append(bar)

    def on_quote_tick(self, tick: Any) -> None:
        raise RuntimeError("tick boom")


class NoHandlerStrategy(FailureIsolatedStrategy):
    """Strategy that does not override any event handler."""

    def __init__(self) -> None:
        self.log = _StubLog()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthyHandlerRunsNormally:

    def test_on_bar_delegates_to_original(self) -> None:
        strat = HealthyStrategy()
        bar = SimpleNamespace(close=100.0)

        strat.on_bar(bar)

        assert strat.bars_received == [bar]
        assert not strat._is_degraded

    def test_on_quote_tick_delegates_to_original(self) -> None:
        strat = HealthyStrategy()
        tick = SimpleNamespace(bid=99.0, ask=101.0)

        strat.on_quote_tick(tick)

        assert strat.ticks_received == [tick]

    def test_no_warnings_or_errors_on_healthy_path(self) -> None:
        strat = HealthyStrategy()
        strat.on_bar(SimpleNamespace())
        strat.on_quote_tick(SimpleNamespace())

        assert strat.log.warnings == []
        assert strat.log.errors == []


class TestOnBarExceptionIsCaughtAndLogged:

    def test_exception_does_not_propagate(self) -> None:
        strat = BuggyBarStrategy()
        # MUST NOT raise
        strat.on_bar(SimpleNamespace())

    def test_strategy_becomes_degraded(self) -> None:
        strat = BuggyBarStrategy()
        strat.on_bar(SimpleNamespace())

        assert strat._is_degraded is True

    def test_error_is_logged(self) -> None:
        strat = BuggyBarStrategy()
        strat.on_bar(SimpleNamespace())

        assert len(strat.log.errors) == 1
        assert "ValueError" in strat.log.errors[0]
        assert "bad bar logic" in strat.log.errors[0]
        assert "degraded" in strat.log.errors[0]


class TestDegradedStrategySkipsSubsequentEvents:

    def test_on_bar_is_noop_when_degraded(self) -> None:
        strat = BuggyBarStrategy()
        strat.on_bar(SimpleNamespace())  # triggers degradation
        assert strat._is_degraded

        strat.on_bar(SimpleNamespace())  # should be a no-op

        # Only one error (the initial), plus one skip warning
        assert len(strat.log.errors) == 1
        assert len(strat.log.warnings) == 1
        assert "skipping" in strat.log.warnings[0].lower()

    def test_multiple_subsequent_calls_all_skipped(self) -> None:
        strat = BuggyBarStrategy()
        strat.on_bar(SimpleNamespace())  # degrade

        for _ in range(5):
            strat.on_bar(SimpleNamespace())

        assert len(strat.log.warnings) == 5
        assert len(strat.log.errors) == 1  # still only the initial


class TestOnQuoteTickExceptionAlsoDegrades:

    def test_tick_exception_degrades_strategy(self) -> None:
        strat = BuggyTickStrategy()

        strat.on_quote_tick(SimpleNamespace())

        assert strat._is_degraded is True
        assert len(strat.log.errors) == 1
        assert "RuntimeError" in strat.log.errors[0]

    def test_degraded_from_tick_also_blocks_on_bar(self) -> None:
        """Once degraded by ANY handler, ALL handlers are blocked."""
        strat = BuggyTickStrategy()
        strat.on_quote_tick(SimpleNamespace())  # degrade via tick
        assert strat._is_degraded

        strat.on_bar(SimpleNamespace())  # should skip

        assert strat.bars_received == []
        assert len(strat.log.warnings) == 1


class TestInitSubclassWrapping:

    def test_wrapped_methods_have_fi_wrapped_marker(self) -> None:
        assert getattr(HealthyStrategy.on_bar, "_fi_wrapped", False) is True
        assert getattr(HealthyStrategy.on_quote_tick, "_fi_wrapped", False) is True

    def test_no_double_wrapping_on_inheritance(self) -> None:
        """A subclass of HealthyStrategy should not re-wrap."""

        class DerivedStrategy(HealthyStrategy):
            pass

        # The method should still have the marker but be the same wrapper
        assert getattr(DerivedStrategy.on_bar, "_fi_wrapped", False) is True

    def test_strategy_without_handlers_does_not_error(self) -> None:
        strat = NoHandlerStrategy()
        # Should not have on_bar at all (or it's the base no-op)
        assert not strat._is_degraded


class TestStrategyDegradedError:

    def test_is_exception_subclass(self) -> None:
        assert issubclass(StrategyDegradedError, Exception)

    def test_message(self) -> None:
        err = StrategyDegradedError("test message")
        assert str(err) == "test message"
