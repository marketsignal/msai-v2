"""MSAI v2 SQLAlchemy ORM models.

All models are imported here so that ``Base.metadata`` contains the full
schema.  This is critical for Alembic autogenerate to detect every table.
"""

from msai.models.asset_universe import AssetUniverse
from msai.models.audit_log import AuditLog
from msai.models.backtest import Backtest
from msai.models.base import Base
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.graduation_stage_transition import GraduationStageTransition
from msai.models.instrument_cache import InstrumentCache
from msai.models.live_deployment import LiveDeployment
from msai.models.live_node_process import LiveNodeProcess
from msai.models.order_attempt_audit import OrderAttemptAudit
from msai.models.portfolio import Portfolio
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.portfolio_run import PortfolioRun
from msai.models.research_job import ResearchJob
from msai.models.research_trial import ResearchTrial
from msai.models.strategy import Strategy
from msai.models.strategy_daily_pnl import StrategyDailyPnl
from msai.models.trade import Trade
from msai.models.user import User

__all__ = [
    "AssetUniverse",
    "AuditLog",
    "Backtest",
    "Base",
    "GraduationCandidate",
    "GraduationStageTransition",
    "InstrumentCache",
    "LiveDeployment",
    "LiveNodeProcess",
    "OrderAttemptAudit",
    "Portfolio",
    "PortfolioAllocation",
    "PortfolioRun",
    "ResearchJob",
    "ResearchTrial",
    "Strategy",
    "StrategyDailyPnl",
    "Trade",
    "User",
]
