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
    """Score a max-drawdown metric so smaller drawdowns rank higher in
    Optuna's ``direction='maximize'`` study.

    ``compute_series_metrics`` stores max drawdown as a NON-POSITIVE float
    (``drawdown.min()`` over the underwater series, e.g., ``-0.20`` for a
    20% drawdown). The original implementation returned ``-max_drawdown``,
    which produces ``+0.20`` for a 20% drawdown and ``+0.05`` for a 5%
    drawdown — Optuna maximizes, so it preferred the WORSE drawdowns.
    Codex-bot PR-73 P1 caught this.

    The correct mapping is ``-abs(max_drawdown)``: a 20% drawdown scores
    ``-0.20`` (low — Optuna avoids), a 5% drawdown scores ``-0.05`` (high
    — Optuna picks). A defensive ``abs`` also handles the unlikely case
    where an upstream regression flips the sign convention to positive.
    """
    return -abs(float(metrics.get("max_drawdown", 0.0)))


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
