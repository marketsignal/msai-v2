from msai.api.account import router as account_router
from msai.api.alerts import router as alerts_router
from msai.api.auth import router as auth_router
from msai.api.backtests import router as backtests_router
from msai.api.graduation import router as graduation_router
from msai.api.live import router as live_router
from msai.api.live_portfolios import router as live_portfolios_router
from msai.api.market_data import router as market_data_router
from msai.api.portfolio import router as portfolio_router
from msai.api.research import router as research_router
from msai.api.strategies import router as strategies_router
from msai.api.strategy_templates import router as strategy_templates_router
from msai.api.websocket import router as websocket_router

__all__ = [
    "account_router",
    "alerts_router",
    "auth_router",
    "backtests_router",
    "graduation_router",
    "live_router",
    "live_portfolios_router",
    "market_data_router",
    "portfolio_router",
    "research_router",
    "strategies_router",
    "strategy_templates_router",
    "websocket_router",
]
