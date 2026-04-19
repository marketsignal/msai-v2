"""Unit tests for ``SmokeMarketOrderStrategy`` (Phase 1 task 1.15).

The strategy is a real Nautilus ``Strategy`` subclass so the usual
test approach — instantiate and call methods directly — doesn't work
without a fully-wired Nautilus engine. Instead we:

1. Import the strategy module to verify it parses and the
   ``SmokeMarketOrderConfig`` is a valid ``StrategyConfig`` subclass
   (Nautilus's ``resolve_config_path`` enforces this at live-start
   time — failing this test is equivalent to a live-start crash).
2. Assert that ``on_bar`` is idempotent: calling it twice on a
   stub strategy instance only submits one order. This is the
   "deterministic" contract the E2E harness (task 1.16) depends on.
3. Assert there is NO custom ``on_stop`` override — the design
   contract is that ``manage_stop=True`` drives the flatten-on-stop
   loop in the base ``Strategy``, so an override here would be a
   regression (Codex plan finding #38, v3 decision #11).

We use ``unittest.mock.MagicMock`` to stub out
``self.order_factory.market`` and ``self.submit_order`` — the
``Strategy`` base doesn't let us call those unless the strategy is
registered with a running trader, but we can short-circuit them in
the test by patching the instance attributes before dispatching to
``on_bar``. This is the same shape the Nautilus project's own unit
tests use.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# ``strategies/`` lives at the claude-version root, not under
# ``backend/src``. Put its parent on sys.path BEFORE the
# ``strategies.example.*`` import so pytest can resolve it at
# collection time. The strategy_registry does this at runtime in
# production; tests that import a strategy directly need to
# replicate the step.
_STRATEGIES_PARENT = str(Path(__file__).resolve().parents[3])
if _STRATEGIES_PARENT not in sys.path:
    sys.path.insert(0, _STRATEGIES_PARENT)

import pytest  # noqa: E402
from nautilus_trader.model.data import BarType  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId  # noqa: E402
from nautilus_trader.trading.config import StrategyConfig  # noqa: E402
from strategies.example.smoke_market_order import (  # noqa: E402
    SmokeMarketOrderConfig,
    SmokeMarketOrderStrategy,
)

# ---------------------------------------------------------------------------
# Config contract
# ---------------------------------------------------------------------------


class TestSmokeConfig:
    def test_config_is_strategyconfig_subclass(self) -> None:
        """Nautilus's ``resolve_config_path`` rejects anything that
        isn't a ``StrategyConfig`` subclass at ``TradingNodeConfig``
        build time. Failing this test means a live start would crash
        inside the subprocess the moment it tried to instantiate the
        strategy."""
        assert issubclass(SmokeMarketOrderConfig, StrategyConfig)

    def test_config_is_frozen(self) -> None:
        """Nautilus strategy configs are frozen msgspec structs so
        the round-trip through ``ImportableStrategyConfig.parse`` is
        reproducible."""
        cfg = SmokeMarketOrderConfig(
            instrument_id=InstrumentId.from_str("AAPL.NASDAQ"),
            bar_type=BarType.from_str("AAPL.NASDAQ-1-MINUTE-LAST-INTERNAL"),
        )
        with pytest.raises((AttributeError, TypeError)):
            cfg.order_id_tag = "mutated"  # type: ignore[misc]

    def test_config_defaults_manage_stop_true(self) -> None:
        """The config's ``manage_stop`` default is True so a manually
        constructed config still gets the flatten-on-stop behavior
        (production always overrides it via Task 1.10's config
        injection but the default matches)."""
        cfg = SmokeMarketOrderConfig(
            instrument_id=InstrumentId.from_str("AAPL.NASDAQ"),
            bar_type=BarType.from_str("AAPL.NASDAQ-1-MINUTE-LAST-INTERNAL"),
        )
        assert cfg.manage_stop is True
        assert cfg.order_id_tag == ""


# ---------------------------------------------------------------------------
# on_bar deterministic single-order contract
# ---------------------------------------------------------------------------


class _StubSmokeStrategy(SmokeMarketOrderStrategy):
    """Test subclass that bypasses Nautilus's Cython ``Strategy.__init__``
    (which requires a live trader registration) and overrides the two
    hook methods Python-level subclassing can safely replace.

    The base ``on_bar`` still runs unchanged — it calls
    ``self._build_market_order()`` and ``self.submit_order(order)``,
    which Python resolves via method-lookup on this subclass first.
    That's the whole point of extracting ``_build_market_order``
    into a helper in Task 1.15.
    """

    def __init__(self) -> None:
        # Do NOT call super().__init__() — the Cython base requires
        # a real trader registration we can't provide in a unit test.
        self.instrument_id = InstrumentId.from_str("AAPL.NASDAQ")
        self.bar_type = BarType.from_str("AAPL.NASDAQ-1-MINUTE-LAST-INTERNAL")
        self._order_submitted = False
        self.submit_calls: list[object] = []
        self.build_calls: int = 0
        self._fake_order = MagicMock(name="fake_order")

    def _build_market_order(self):  # type: ignore[override, no-untyped-def]
        self.build_calls += 1
        return self._fake_order

    def submit_order(self, order, *args, **kwargs) -> None:  # type: ignore[override, no-untyped-def]  # noqa: ARG002
        self.submit_calls.append(order)


class TestSmokeOnBar:
    def test_first_bar_submits_exactly_one_order(self) -> None:
        strat = _StubSmokeStrategy()
        strat.on_bar(bar=MagicMock())

        assert strat.build_calls == 1
        assert len(strat.submit_calls) == 1
        assert strat.submit_calls[0] is strat._fake_order
        assert strat._order_submitted is True

    def test_second_bar_does_not_submit_additional_order(self) -> None:
        """Determinism guard: subsequent bars must NOT produce
        additional orders. This is what the E2E harness counts on
        when asserting "exactly one row in order_attempt_audits"."""
        strat = _StubSmokeStrategy()
        strat.on_bar(bar=MagicMock())
        strat.on_bar(bar=MagicMock())
        strat.on_bar(bar=MagicMock())

        assert strat.build_calls == 1
        assert len(strat.submit_calls) == 1


# ---------------------------------------------------------------------------
# No custom on_stop (Codex plan finding #38 / v3 decision #11)
# ---------------------------------------------------------------------------


class TestNoCustomOnStop:
    def test_on_stop_is_inherited_from_base_strategy(self) -> None:
        """The design contract is that ``manage_stop=True`` drives
        the flatten-on-stop loop in Nautilus's ``Strategy`` base.
        A custom override here would race the engine's own shutdown
        and re-introduce gotcha #13. This test asserts that
        ``on_stop`` is NOT defined on ``SmokeMarketOrderStrategy``
        itself — it comes from the base class."""
        assert "on_stop" not in SmokeMarketOrderStrategy.__dict__
