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


def test_objective_score_max_drawdown_negated():
    """Optuna maximizes — we negate max_drawdown so 'less negative' wins."""
    metrics = {"max_drawdown": -0.20}
    assert objective_score(metrics, PortfolioObjective.MINIMIZE_MAX_DRAWDOWN) == 0.20


def test_objective_score_calmar():
    metrics = {"total_return": 0.20, "max_drawdown": -0.10}
    # Calmar = annualized_return / abs(max_dd)
    assert objective_score(metrics, PortfolioObjective.MAXIMIZE_CALMAR) == 2.0
