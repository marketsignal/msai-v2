"""IB account data queries via ib_async.

Provides an escape hatch to query Interactive Brokers Gateway directly
for account summary and portfolio data.  In Phase 1 the methods return
mock data; the real ``ib_async`` connection will be wired in Phase 2
once the IB Gateway container is running in the Docker stack.
"""

from __future__ import annotations

from msai.core.logging import get_logger

log = get_logger(__name__)


class IBAccountService:
    """Queries IB Gateway for account data.

    Args:
        host: IB Gateway hostname (default ``"ib-gateway"`` for Docker).
        port: IB Gateway API port (default ``4002`` for paper trading).
    """

    def __init__(self, host: str = "ib-gateway", port: int = 4002) -> None:
        self.host = host
        self.port = port

    async def get_summary(self) -> dict[str, float]:
        """Return an account summary with key financial metrics.

        Returns:
            Dictionary with keys such as ``net_liquidation``,
            ``buying_power``, ``margin_used``, etc.

        TODO: Connect via ib_async when IB Gateway is running.
        """
        log.debug("ib_account_summary_requested", host=self.host, port=self.port)
        return {
            "net_liquidation": 125_430.56,
            "buying_power": 250_000.00,
            "margin_used": 15_000.00,
            "available_funds": 110_430.56,
            "unrealized_pnl": 1_234.56,
            "realized_pnl": 5_678.90,
        }

    async def get_portfolio(self) -> list[dict[str, object]]:
        """Return current portfolio positions.

        Returns:
            List of position dictionaries.  Empty in Phase 1.

        TODO: Connect via ib_async when IB Gateway is running.
        """
        log.debug("ib_portfolio_requested", host=self.host, port=self.port)
        return []
