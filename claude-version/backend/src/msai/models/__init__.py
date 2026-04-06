"""MSAI v2 SQLAlchemy ORM models.

All models are imported here so that ``Base.metadata`` contains the full
schema.  This is critical for Alembic autogenerate to detect every table.
"""

from msai.models.base import Base
from msai.models.user import User
from msai.models.strategy import Strategy
from msai.models.backtest import Backtest
from msai.models.live_deployment import LiveDeployment
from msai.models.trade import Trade
from msai.models.strategy_daily_pnl import StrategyDailyPnl
from msai.models.audit_log import AuditLog

__all__ = [
    "Base",
    "User",
    "Strategy",
    "Backtest",
    "LiveDeployment",
    "Trade",
    "StrategyDailyPnl",
    "AuditLog",
]
