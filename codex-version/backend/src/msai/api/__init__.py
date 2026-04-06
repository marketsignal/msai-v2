from msai.api.account import router as account_router
from msai.api.auth import router as auth_router
from msai.api.backtests import router as backtests_router
from msai.api.live import router as live_router
from msai.api.market_data import router as market_data_router
from msai.api.strategies import router as strategies_router
from msai.api.websocket import router as websocket_router

__all__ = [
    "account_router",
    "auth_router",
    "backtests_router",
    "live_router",
    "market_data_router",
    "strategies_router",
    "websocket_router",
]
