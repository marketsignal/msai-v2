"""Tests for the B2 extensions to portfolio enums.

Verifies:
- ``PortfolioObjective`` gains ``MAXIMIZE_CALMAR`` + ``MINIMIZE_MAX_DRAWDOWN``.
- ``PortfolioRunStatus`` gains ``CANCELED`` and treats it as terminal.
- The new ``BacktestMode`` enum (Quick / Full) exists with the expected
  string values used as ``StrEnum`` mixins for raw-SQL compatibility.
"""

from __future__ import annotations

from msai.models.portfolio_enums import (
    BacktestMode,
    PortfolioObjective,
    PortfolioRunStatus,
)


def test_new_objectives_present() -> None:
    assert PortfolioObjective.MAXIMIZE_CALMAR == "maximize_calmar"
    assert PortfolioObjective.MINIMIZE_MAX_DRAWDOWN == "minimize_max_drawdown"


def test_new_run_status_present() -> None:
    assert PortfolioRunStatus.CANCELED == "canceled"
    # is_terminal stays correct
    assert PortfolioRunStatus.CANCELED.is_terminal


def test_backtest_mode_enum() -> None:
    assert BacktestMode.QUICK == "quick"
    assert BacktestMode.FULL == "full"
