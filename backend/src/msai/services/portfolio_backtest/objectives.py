"""Objective function registry — maps PortfolioObjective to a metrics-dict scorer.

Optuna maximizes; objectives intended to minimize (max_drawdown) are negated so
the optimizer can maximize uniformly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from msai.models.portfolio_enums import PortfolioObjective

if TYPE_CHECKING:
    from collections.abc import Callable


def _score_total_return(metrics: dict[str, Any]) -> float:
    return float(metrics.get("total_return", 0.0))


def _score_sharpe(metrics: dict[str, Any]) -> float:
    return float(metrics.get("sharpe", 0.0))


def _score_sortino(metrics: dict[str, Any]) -> float:
    return float(metrics.get("sortino", 0.0))


def _score_calmar(metrics: dict[str, Any]) -> float:
    ann_return = float(metrics.get("total_return", 0.0))
    max_dd = float(metrics.get("max_drawdown", 0.0))
    if max_dd == 0:
        return 0.0
    return ann_return / abs(max_dd)


def _score_negative_max_drawdown(metrics: dict[str, Any]) -> float:
    """Return |max_drawdown| negated to a positive score (higher = better)."""
    return -float(metrics.get("max_drawdown", 0.0))


OBJECTIVES: dict[PortfolioObjective, Callable[[dict[str, Any]], float]] = {
    PortfolioObjective.MAXIMIZE_PROFIT: _score_total_return,
    PortfolioObjective.MAXIMIZE_SHARPE: _score_sharpe,
    PortfolioObjective.MAXIMIZE_SORTINO: _score_sortino,
    PortfolioObjective.MAXIMIZE_CALMAR: _score_calmar,
    PortfolioObjective.MINIMIZE_MAX_DRAWDOWN: _score_negative_max_drawdown,
}


def objective_score(metrics: dict[str, Any], obj: PortfolioObjective) -> float:
    if obj not in OBJECTIVES:
        raise ValueError(f"unknown objective {obj!r}")
    return OBJECTIVES[obj](metrics)
