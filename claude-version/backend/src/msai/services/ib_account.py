"""IB account data queries via ib_async.

Connects to IB Gateway for account summary and portfolio data.
Falls back to zero-valued responses if IB Gateway is unreachable
or ib_async is not installed (e.g., running without the live profile).
"""

from __future__ import annotations

from typing import Any

from msai.core.logging import get_logger

log = get_logger(__name__)

try:
    from ib_async import IB
except ImportError:
    IB = None  # type: ignore[assignment,misc]

# Dedicated client ID for read-only account queries.
# 0 is reserved by IB as the master client; live_node_config derives
# per-deployment IDs from the deployment_slug hash. 99 is safe.
_ACCOUNT_CLIENT_ID = 99

_ZERO_SUMMARY: dict[str, float] = {
    "net_liquidation": 0.0,
    "buying_power": 0.0,
    "margin_used": 0.0,
    "available_funds": 0.0,
    "unrealized_pnl": 0.0,
    "realized_pnl": 0.0,
}


class IBAccountService:
    """Queries IB Gateway for account data.

    Falls back gracefully to zero-valued responses if IB Gateway
    is unreachable or ``ib_async`` is not installed.

    Args:
        host: IB Gateway hostname (default from settings).
        port: IB Gateway API port (default ``4002`` for paper trading).
    """

    def __init__(self, host: str = "ib-gateway", port: int = 4002) -> None:
        self.host = host
        self.port = port

    async def get_summary(self) -> dict[str, float]:
        """Return account summary. Zero-valued dict if IB unreachable."""
        if IB is None:
            log.debug("ib_async_not_installed")
            return dict(_ZERO_SUMMARY)

        ib = IB()
        try:
            await ib.connectAsync(
                self.host, self.port, clientId=_ACCOUNT_CLIENT_ID, timeout=5
            )
            tags = await ib.accountSummaryAsync()
            result = dict(_ZERO_SUMMARY)
            tag_map = {
                "NetLiquidation": "net_liquidation",
                "BuyingPower": "buying_power",
                "TotalCashValue": "available_funds",
                "MaintMarginReq": "margin_used",
                "UnrealizedPnL": "unrealized_pnl",
                "RealizedPnL": "realized_pnl",
            }
            for item in tags:
                key = tag_map.get(item.tag)
                if key:
                    try:
                        result[key] = float(item.value)
                    except (ValueError, TypeError):
                        pass
            return result
        except Exception:
            log.warning("ib_account_summary_failed", host=self.host, port=self.port)
            return dict(_ZERO_SUMMARY)
        finally:
            ib.disconnect()

    async def get_portfolio(self) -> list[dict[str, Any]]:
        """Return current IB portfolio positions. Empty list if unreachable."""
        if IB is None:
            return []

        ib = IB()
        try:
            await ib.connectAsync(
                self.host, self.port, clientId=_ACCOUNT_CLIENT_ID, timeout=5
            )
            positions = ib.portfolio()
            return [
                {
                    "symbol": p.contract.symbol,
                    "sec_type": p.contract.secType,
                    "position": float(p.position),
                    "market_price": float(p.marketPrice),
                    "market_value": float(p.marketValue),
                    "average_cost": float(p.averageCost),
                    "unrealized_pnl": float(p.unrealizedPNL),
                    "realized_pnl": float(p.realizedPNL),
                }
                for p in positions
            ]
        except Exception:
            log.warning("ib_portfolio_failed", host=self.host, port=self.port)
            return []
        finally:
            ib.disconnect()
