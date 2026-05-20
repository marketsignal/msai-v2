"""Tests for the B4 ``PortfolioRun`` extensions.

Verifies the new ``mode``, ``optimization_trace``, ``walk_forward_payload``,
``is_metric``, and ``oos_metric`` columns round-trip through the ORM
constructor.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from msai.models.portfolio_enums import BacktestMode
from msai.models.portfolio_run import PortfolioRun


def test_portfolio_run_has_mode_and_trace() -> None:
    run = PortfolioRun(
        id=uuid4(),
        portfolio_id=uuid4(),
        status="pending",
        start_date=date(2024, 1, 1),
        end_date=date(2026, 1, 1),
        mode=BacktestMode.FULL,
        optimization_trace=[{"trial": 0, "value": 1.23, "params": {"x": 1}}],
        walk_forward_payload={"windows": []},
        is_metric=1.45,
        oos_metric=1.12,
    )
    assert run.mode == BacktestMode.FULL
    assert run.optimization_trace is not None
    assert run.optimization_trace[0]["value"] == 1.23
    assert run.is_metric == 1.45
    assert run.oos_metric == 1.12
