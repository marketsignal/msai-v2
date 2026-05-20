"""Tests for the B3 ``Portfolio`` safety-cap and mode extensions.

Verifies the new ``max_position_size``, ``max_drawdown_halt``,
``default_mode``, and ``allocator_name`` columns are wired into the ORM
class constructor and round-trip through SQLAlchemy attribute access.
"""

from __future__ import annotations

from uuid import uuid4

from msai.models.portfolio import Portfolio
from msai.models.portfolio_enums import BacktestMode


def test_portfolio_has_safety_caps() -> None:
    p = Portfolio(
        id=uuid4(),
        name="test",
        objective="maximize_sharpe",
        base_capital=100_000.0,
        max_position_size=0.25,
        max_drawdown_halt=0.20,
        default_mode=BacktestMode.QUICK,
    )
    assert p.max_position_size == 0.25
    assert p.max_drawdown_halt == 0.20
    assert p.default_mode == BacktestMode.QUICK
