from msai.models.portfolio_enums import PortfolioObjective
from msai.services.portfolio_backtest.objectives import OBJECTIVES, objective_score


def test_registry_has_five_objectives():
    assert set(OBJECTIVES.keys()) == {
        PortfolioObjective.MAXIMIZE_PROFIT,
        PortfolioObjective.MAXIMIZE_SHARPE,
        PortfolioObjective.MAXIMIZE_SORTINO,
        PortfolioObjective.MAXIMIZE_CALMAR,
        PortfolioObjective.MINIMIZE_MAX_DRAWDOWN,
    }


def test_objective_score_maximize_sharpe():
    metrics = {"sharpe": 1.5, "sortino": 1.8, "total_return": 0.20, "max_drawdown": -0.10}
    assert objective_score(metrics, PortfolioObjective.MAXIMIZE_SHARPE) == 1.5


def test_objective_score_minimize_max_drawdown_prefers_smaller_drawdowns():
    """Codex-bot PR-73 P1 regression — under Optuna's ``direction='maximize'``,
    smaller drawdowns MUST score higher than larger drawdowns.

    ``compute_series_metrics`` stores max drawdown as a non-positive value
    (``drawdown.min()``). The original implementation returned
    ``-max_drawdown`` which made a 20% drawdown score ``+0.20`` (high) and a
    5% drawdown score ``+0.05`` (low) — Optuna would have maximized the
    WORST drawdown. The correct mapping is ``-abs(max_drawdown)``: 20% DD
    scores ``-0.20`` (low), 5% DD scores ``-0.05`` (high).
    """
    big_dd = {"max_drawdown": -0.20}
    small_dd = {"max_drawdown": -0.05}

    big_score = objective_score(big_dd, PortfolioObjective.MINIMIZE_MAX_DRAWDOWN)
    small_score = objective_score(small_dd, PortfolioObjective.MINIMIZE_MAX_DRAWDOWN)

    assert big_score == -0.20
    assert small_score == -0.05
    # The load-bearing assertion: under Optuna maximization, smaller DD
    # MUST produce a higher (less negative) score than larger DD.
    assert small_score > big_score, (
        "MINIMIZE_MAX_DRAWDOWN must prefer smaller drawdowns under Optuna maximize"
    )


def test_objective_score_minimize_max_drawdown_handles_positive_input():
    """Defensive: if an upstream regression flips the sign convention to
    positive, the objective still scores smaller drawdowns higher."""
    big_dd = {"max_drawdown": 0.20}  # bug: positive instead of non-positive
    small_dd = {"max_drawdown": 0.05}

    assert objective_score(big_dd, PortfolioObjective.MINIMIZE_MAX_DRAWDOWN) == -0.20
    assert objective_score(small_dd, PortfolioObjective.MINIMIZE_MAX_DRAWDOWN) == -0.05
    assert objective_score(small_dd, PortfolioObjective.MINIMIZE_MAX_DRAWDOWN) > objective_score(
        big_dd, PortfolioObjective.MINIMIZE_MAX_DRAWDOWN
    )


def test_objective_score_calmar():
    metrics = {"total_return": 0.20, "max_drawdown": -0.10}
    # Calmar = annualized_return / abs(max_dd)
    assert objective_score(metrics, PortfolioObjective.MAXIMIZE_CALMAR) == 2.0
