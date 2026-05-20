"""Smoke test — real Nautilus Cache positions(strategy_id=) filter works as documented.

This is the integration version of the Approach Comparison's cheapest falsifying
test. We instantiate a minimal Nautilus BacktestEngine with 2 strategies, feed a
handful of bars, and assert the Cache filter splits positions correctly.

Marked ``slow`` because it spins up Nautilus state. Marked ``xfail`` while the
2-strategy mini-backtest fixture is unbuilt — the contract is locked by the
unit-test stub in
``backend/tests/unit/services/portfolio_backtest/test_per_strategy_attribution.py``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.slow


def test_cache_positions_strategy_id_filter() -> None:
    """Run a 2-strategy mini-backtest and assert Cache filters correctly."""
    pytest.importorskip("nautilus_trader")
    # Subagent / future work: assemble a 2-strategy minimal-bars fixture using
    # TestInstrumentProvider patterns; assert engine.cache.positions(strategy_id="s1")
    # and (strategy_id="s2") return disjoint position lists and their sums equal
    # the total. The unit-test stub above validates the contract we depend on.
    pytest.xfail(
        "Nautilus 2-strategy mini-backtest fixture not yet built — spike landed in unit form."
    )
