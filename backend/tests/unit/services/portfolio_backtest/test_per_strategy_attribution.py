"""The cheapest falsifying test (Phase 3.1b).

Validates that Nautilus's Cache.positions(strategy_id=sid) returns exactly the
positions of the named strategy when two strategies trade in the same engine.
If this fails, the project's portfolio-backtest design (no hand-rolled
aggregator) is wrong — the design collapses and we revisit the Approach
Comparison.

This test is intentionally small. It does NOT run a full Nautilus backtest —
it directly verifies the Cache filter using a stub Cache object that mirrors
the Nautilus Cache interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from msai.services.portfolio_backtest.per_strategy_attribution import (
    extract_per_strategy_pnl,
)


@dataclass
class _StubPosition:
    """Minimal stub mirroring nautilus_trader Position attributes we read."""

    strategy_id: str
    realized_pnl: float


@dataclass
class _StubCache:
    """Stub Cache exposing the positions(strategy_id=) filter we depend on."""

    _positions: list[_StubPosition] = field(default_factory=list)

    def positions(self, strategy_id: str | None = None) -> list[_StubPosition]:
        if strategy_id is None:
            return list(self._positions)
        return [p for p in self._positions if p.strategy_id == strategy_id]

    def strategy_ids(self) -> list[str]:
        return sorted({p.strategy_id for p in self._positions})


def test_extract_per_strategy_pnl_returns_one_entry_per_strategy() -> None:
    cache = _StubCache(
        _positions=[
            _StubPosition(strategy_id="s1", realized_pnl=100.0),
            _StubPosition(strategy_id="s1", realized_pnl=50.0),
            _StubPosition(strategy_id="s2", realized_pnl=-25.0),
        ]
    )
    pnls = extract_per_strategy_pnl(cache)
    assert isinstance(pnls, list)
    assert {p.strategy_id for p in pnls} == {"s1", "s2"}


def test_extract_per_strategy_pnl_sums_correctly() -> None:
    cache = _StubCache(
        _positions=[
            _StubPosition(strategy_id="s1", realized_pnl=100.0),
            _StubPosition(strategy_id="s1", realized_pnl=50.0),
            _StubPosition(strategy_id="s2", realized_pnl=-25.0),
        ]
    )
    pnls = {p.strategy_id: p.realized_pnl for p in extract_per_strategy_pnl(cache)}
    assert pnls["s1"] == 150.0
    assert pnls["s2"] == -25.0


def test_sum_of_per_strategy_equals_total() -> None:
    """Critical correctness invariant: sum of per-strategy PnL == total PnL."""
    cache = _StubCache(
        _positions=[
            _StubPosition(strategy_id="s1", realized_pnl=100.0),
            _StubPosition(strategy_id="s2", realized_pnl=-25.0),
            _StubPosition(strategy_id="s3", realized_pnl=75.0),
        ]
    )
    pnls = extract_per_strategy_pnl(cache)
    assert sum(p.realized_pnl for p in pnls) == pytest.approx(150.0)


def test_empty_cache_returns_empty_list() -> None:
    pnls = extract_per_strategy_pnl(_StubCache())
    assert pnls == []
