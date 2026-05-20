"""Tests for the B5 ``PortfolioCreate`` / ``PortfolioRunCreate`` extensions.

Verifies:
- ``PortfolioCreate`` accepts the new safety-cap fields and validates ranges.
- ``PortfolioCreate`` accepts the new ``default_mode`` + ``allocator_name``.
- ``PortfolioRunCreate`` accepts ``mode`` and defaults it to Quick.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from msai.models.portfolio_enums import BacktestMode, PortfolioObjective
from msai.schemas.portfolio import (
    PortfolioAllocationInput,
    PortfolioCreate,
    PortfolioRunCreate,
)


def test_portfolio_create_accepts_safety_caps() -> None:
    p = PortfolioCreate(
        name="x",
        objective=PortfolioObjective.MAXIMIZE_SHARPE,
        base_capital=100_000.0,
        requested_leverage=1.0,
        # F1c: exactly one of strategy_ids OR allocations is required;
        # pass a minimal allocation so the validator passes.
        allocations=[PortfolioAllocationInput(candidate_id=uuid4(), weight=1.0)],
        max_position_size=0.25,
        max_drawdown_halt=0.20,
        default_mode=BacktestMode.QUICK,
    )
    assert p.max_position_size == 0.25
    assert p.max_drawdown_halt == 0.20
    assert p.default_mode == BacktestMode.QUICK


def test_portfolio_create_rejects_invalid_max_position_size() -> None:
    with pytest.raises(ValueError):
        PortfolioCreate(
            name="x",
            objective=PortfolioObjective.MAXIMIZE_SHARPE,
            base_capital=100_000.0,
            allocations=[PortfolioAllocationInput(candidate_id=uuid4(), weight=1.0)],
            max_position_size=1.5,  # >1 invalid
        )


def test_portfolio_run_create_accepts_mode() -> None:
    rc = PortfolioRunCreate(
        portfolio_id="00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
        start_date="2024-01-01",  # type: ignore[arg-type]
        end_date="2025-01-01",  # type: ignore[arg-type]
        mode=BacktestMode.FULL,
    )
    assert rc.mode == BacktestMode.FULL


def test_portfolio_run_create_mode_defaults_to_none_for_inheritance() -> None:
    """Codex-bot PR-73 P2 regression — schema default is None ('inherit
    from portfolio.default_mode'), not QUICK. The previous QUICK default
    silently overrode a portfolio's default_mode='full' whenever a client
    omitted the field. The lifecycle service resolves the inheritance at
    persist time."""
    rc = PortfolioRunCreate(
        portfolio_id="00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
        start_date="2024-01-01",  # type: ignore[arg-type]
        end_date="2025-01-01",  # type: ignore[arg-type]
    )
    assert rc.mode is None


def test_portfolio_run_create_mode_explicit_quick_still_works() -> None:
    """Explicit mode=quick remains supported alongside the new None default."""
    rc = PortfolioRunCreate(
        portfolio_id="00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
        start_date="2024-01-01",  # type: ignore[arg-type]
        end_date="2025-01-01",  # type: ignore[arg-type]
        mode=BacktestMode.QUICK,
    )
    assert rc.mode is BacktestMode.QUICK


def test_portfolio_run_create_rejects_full_mode_below_minimum_range() -> None:
    """Bug 2 regression -- Full-mode runs require >= 90 days between
    start_date and end_date.

    Shorter ranges raise ``ValueError("No walk-forward windows fit ...")``
    inside the worker even after the orchestrator's adaptive scaler
    floors each leg at 30 days; the schema rejects them at the API
    boundary so callers get a precise 422 instead of a generic 500.
    """
    with pytest.raises(ValueError, match="[Ff]ull mode requires"):
        PortfolioRunCreate(
            portfolio_id="00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
            start_date="2024-01-01",  # type: ignore[arg-type]
            end_date="2024-01-31",  # type: ignore[arg-type]
            mode=BacktestMode.FULL,
        )


def test_portfolio_run_create_accepts_full_mode_at_minimum_range() -> None:
    """A 90-day range (start..start+89 inclusive) is the smallest accepted
    Full-mode range -- exactly at the orchestrator's scaling floor."""
    rc = PortfolioRunCreate(
        portfolio_id="00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
        start_date="2024-01-01",  # type: ignore[arg-type]
        end_date="2024-03-30",  # type: ignore[arg-type] -- 90 inclusive days
        mode=BacktestMode.FULL,
    )
    assert rc.mode == BacktestMode.FULL


def test_portfolio_run_create_quick_mode_accepts_short_range() -> None:
    """Quick mode is single-shot — any inclusive range stays valid (the
    minimum-range gate only applies to Full mode)."""
    rc = PortfolioRunCreate(
        portfolio_id="00000000-0000-0000-0000-000000000000",  # type: ignore[arg-type]
        start_date="2024-01-01",  # type: ignore[arg-type]
        end_date="2024-01-05",  # type: ignore[arg-type]  -- 5 days, well below 90
        mode=BacktestMode.QUICK,
    )
    assert rc.mode == BacktestMode.QUICK


def test_portfolio_create_rejects_full_mode_with_fixed_weight_allocator() -> None:
    """Phase 5.1 P1-E — Full mode + ``fixed_weight`` allocator is rejected.

    ``fixed_weight`` requires operator-supplied weights at construction
    time and cannot be auto-derived per Optuna trial; the schema must
    raise at the boundary so the caller learns immediately instead of
    silently degrading to equal-weight inside the trial body.
    """
    with pytest.raises(ValueError, match="[Ff]ull mode"):
        PortfolioCreate(
            name="test",
            objective=PortfolioObjective.MAXIMIZE_SHARPE,
            base_capital=100_000.0,
            default_mode=BacktestMode.FULL,
            allocator_name="fixed_weight",
            allocations=[PortfolioAllocationInput(candidate_id=uuid4(), weight=1.0)],
        )


def test_portfolio_run_create_rejects_reversed_date_range() -> None:
    """Codex bot iter-14 P2 on PR #73 — reject ``end_date < start_date``
    at the schema layer for BOTH modes. An inverted range is meaningless
    and would otherwise enqueue work that fails later in the worker
    with a less actionable error.
    """
    from msai.schemas.portfolio import PortfolioRunCreate

    with pytest.raises(ValueError, match="end_date.*must be.*start_date"):
        PortfolioRunCreate(
            portfolio_id=uuid4(),
            start_date=date(2024, 6, 30),
            end_date=date(2024, 1, 1),  # reversed
            mode=BacktestMode.QUICK,
        )

    # And the same rejection in Full mode.
    with pytest.raises(ValueError, match="end_date.*must be.*start_date"):
        PortfolioRunCreate(
            portfolio_id=uuid4(),
            start_date=date(2024, 6, 30),
            end_date=date(2024, 1, 1),  # reversed
            mode=BacktestMode.FULL,
        )
