"""MSAI v2 SQLAlchemy ORM models.

All models are imported here so that ``Base.metadata`` contains the full
schema.  This is critical for Alembic autogenerate to detect every table.
"""

from msai.models.audit_log import AuditLog
from msai.models.backtest import Backtest
from msai.models.base import Base
from msai.models.instrument_cache import InstrumentCache
from msai.models.live_deployment import LiveDeployment
from msai.models.live_node_process import LiveNodeProcess
from msai.models.order_attempt_audit import OrderAttemptAudit
from msai.models.strategy import Strategy
from msai.models.strategy_daily_pnl import StrategyDailyPnl
from msai.models.trade import Trade
from msai.models.user import User

__all__ = [
    "AuditLog",
    "Backtest",
    "Base",
    "InstrumentCache",
    "LiveDeployment",
    "LiveNodeProcess",
    "OrderAttemptAudit",
    "Strategy",
    "StrategyDailyPnl",
    "Trade",
    "User",
]
