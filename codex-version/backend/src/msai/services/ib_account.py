from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from msai.core.config import settings
from msai.core.logging import get_logger

logger = get_logger("ib_account")


@dataclass(slots=True)
class AccountSummary:
    net_liquidation: float
    buying_power: float
    margin_used: float
    available_funds: float
    unrealized_pnl: float


class IBAccountService:
    def __init__(self) -> None:
        from ib_async import IB

        self._ib = IB()
        self._mock_mode = False

    async def connect(self) -> None:
        if self._ib.isConnected():
            return

        try:
            await self._ib.connectAsync(
                host=settings.ib_gateway_host,
                port=settings.ib_gateway_port_paper,
                clientId=settings.ib_client_id,
                timeout=settings.ib_connect_timeout_seconds,
                account=settings.ib_account_id or "",
            )
            logger.info("ib_connected", host=settings.ib_gateway_host, port=settings.ib_gateway_port_paper)
            self._mock_mode = False
        except Exception as exc:
            if settings.ib_allow_mock_fallback and settings.environment != "production":
                logger.warning("ib_connect_failed_fallback", error=str(exc))
                self._mock_mode = True
                return
            raise

    async def summary(self) -> AccountSummary:
        await self.connect()

        if self._mock_mode:
            return AccountSummary(
                net_liquidation=1_250_000.0,
                buying_power=650_000.0,
                margin_used=200_000.0,
                available_funds=450_000.0,
                unrealized_pnl=1_540.0,
            )

        values = await self._ib.accountSummaryAsync(account=settings.ib_account_id or "")
        by_tag = {item.tag: item for item in values}

        net_liquidation = _to_float(by_tag.get("NetLiquidation"))
        buying_power = _to_float(by_tag.get("BuyingPower"))
        available_funds = _to_float(by_tag.get("AvailableFunds"))
        unrealized_pnl = _to_float(by_tag.get("UnrealizedPnL"))
        margin_used = max(0.0, net_liquidation - available_funds)

        return AccountSummary(
            net_liquidation=net_liquidation,
            buying_power=buying_power,
            margin_used=margin_used,
            available_funds=available_funds,
            unrealized_pnl=unrealized_pnl,
        )

    async def portfolio(self) -> list[dict[str, float | str]]:
        await self.connect()

        if self._mock_mode:
            return [
                {
                    "instrument": "AAPL",
                    "quantity": 25.0,
                    "avg_price": 212.4,
                    "market_value": 5360.0,
                    "unrealized_pnl": 45.0,
                }
            ]

        rows: list[dict[str, float | str]] = []
        for item in self._ib.portfolio(account=settings.ib_account_id or ""):
            symbol = getattr(item.contract, "localSymbol", None) or getattr(item.contract, "symbol", "UNKNOWN")
            rows.append(
                {
                    "instrument": str(symbol),
                    "quantity": float(item.position),
                    "avg_price": float(item.averageCost),
                    "market_value": float(item.marketValue),
                    "unrealized_pnl": float(item.unrealizedPNL),
                }
            )
        return rows

    async def health(self) -> dict[str, str | bool]:
        await self.connect()

        if self._mock_mode:
            return {"status": "degraded", "connected": False, "mock_mode": True}

        connected = bool(self._ib.isConnected())
        return {
            "status": "ok" if connected else "degraded",
            "connected": connected,
            "mock_mode": False,
        }


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    raw = getattr(value, "value", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


ib_account_service = IBAccountService()
