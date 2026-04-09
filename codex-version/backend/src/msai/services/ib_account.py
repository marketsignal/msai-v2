from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
    equity_with_loan_value: float = 0.0
    initial_margin_requirement: float = 0.0
    maintenance_margin_requirement: float = 0.0
    excess_liquidity: float = 0.0
    sma: float = 0.0
    gross_position_value: float = 0.0
    cushion: float = 0.0


@dataclass(slots=True)
class BrokerSnapshot:
    connected: bool
    mock_mode: bool
    generated_at: str
    positions: list[dict[str, Any]]
    open_orders: list[dict[str, Any]]


@dataclass(slots=True)
class _ClientState:
    ib: Any
    mock_mode: bool = False


class IBAccountService:
    def __init__(self) -> None:
        from ib_async import IB

        self._client_factory = IB
        self._states: dict[bool, _ClientState] = {
            True: _ClientState(ib=IB()),
            False: _ClientState(ib=IB()),
        }

    def _state(self, paper_trading: bool) -> _ClientState:
        state = self._states.get(paper_trading)
        if state is None:
            state = _ClientState(ib=self._client_factory())
            self._states[paper_trading] = state
        return state

    async def connect(self, *, paper_trading: bool = True) -> _ClientState:
        state = self._state(paper_trading)
        if state.ib.isConnected():
            return state

        port = settings.ib_gateway_port_paper if paper_trading else settings.ib_gateway_port_live
        try:
            await state.ib.connectAsync(
                host=settings.ib_gateway_host,
                port=port,
                clientId=settings.ib_client_id,
                timeout=settings.ib_connect_timeout_seconds,
                account=settings.ib_account_id or "",
            )
            logger.info(
                "ib_connected",
                host=settings.ib_gateway_host,
                port=port,
                paper_trading=paper_trading,
            )
            state.mock_mode = False
        except Exception as exc:
            if settings.ib_allow_mock_fallback and settings.environment != "production":
                logger.warning(
                    "ib_connect_failed_fallback",
                    error=str(exc),
                    paper_trading=paper_trading,
                )
                state.mock_mode = True
                return state
            raise
        return state

    async def summary(self, *, paper_trading: bool = True) -> AccountSummary:
        state = await self.connect(paper_trading=paper_trading)

        if state.mock_mode:
            return AccountSummary(
                net_liquidation=1_250_000.0,
                buying_power=650_000.0,
                margin_used=200_000.0,
                available_funds=450_000.0,
                unrealized_pnl=1_540.0,
                equity_with_loan_value=1_250_000.0,
                initial_margin_requirement=200_000.0,
                maintenance_margin_requirement=180_000.0,
                excess_liquidity=430_000.0,
                sma=50_000.0,
                gross_position_value=800_000.0,
                cushion=0.344,
            )

        values = await state.ib.accountSummaryAsync(account=settings.ib_account_id or "")
        by_tag = {item.tag: item for item in values}

        net_liquidation = _to_float(by_tag.get("NetLiquidation"))
        equity_with_loan_value = _to_float(by_tag.get("EquityWithLoanValue")) or net_liquidation
        buying_power = _to_float(by_tag.get("BuyingPower"))
        available_funds = _to_float(by_tag.get("AvailableFunds"))
        excess_liquidity = _to_float(by_tag.get("ExcessLiquidity"))
        unrealized_pnl = _to_float(by_tag.get("UnrealizedPnL"))
        initial_margin_requirement = _to_float(by_tag.get("InitMarginReq"))
        maintenance_margin_requirement = _to_float(by_tag.get("MaintMarginReq"))
        sma = _to_float(by_tag.get("SMA"))
        gross_position_value = _to_float(by_tag.get("GrossPositionValue"))
        cushion = _to_float(by_tag.get("Cushion"))
        margin_used = initial_margin_requirement or max(0.0, net_liquidation - available_funds)

        return AccountSummary(
            net_liquidation=net_liquidation,
            buying_power=buying_power,
            margin_used=margin_used,
            available_funds=available_funds,
            unrealized_pnl=unrealized_pnl,
            equity_with_loan_value=equity_with_loan_value,
            initial_margin_requirement=initial_margin_requirement,
            maintenance_margin_requirement=maintenance_margin_requirement,
            excess_liquidity=excess_liquidity,
            sma=sma,
            gross_position_value=gross_position_value,
            cushion=cushion,
        )

    async def portfolio(self, *, paper_trading: bool = True) -> list[dict[str, float | str]]:
        state = await self.connect(paper_trading=paper_trading)

        if state.mock_mode:
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
        for item in state.ib.portfolio(account=settings.ib_account_id or ""):
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

    async def reconciliation_snapshot(self, *, paper_trading: bool = True) -> BrokerSnapshot:
        state = await self.connect(paper_trading=paper_trading)
        generated_at = datetime.now(UTC).isoformat()

        if state.mock_mode:
            return BrokerSnapshot(
                connected=False,
                mock_mode=True,
                generated_at=generated_at,
                positions=[],
                open_orders=[],
            )

        await state.ib.reqPositionsAsync()
        open_trades = await state.ib.reqAllOpenOrdersAsync()

        account_id = settings.ib_account_id or ""
        portfolio_by_con_id: dict[int, Any] = {}
        for item in state.ib.portfolio(account=account_id):
            con_id = _to_optional_int(getattr(item.contract, "conId", None))
            if con_id is not None:
                portfolio_by_con_id[con_id] = item

        positions: list[dict[str, Any]] = []
        for item in state.ib.positions(account=account_id):
            contract = item.contract
            con_id = _to_optional_int(getattr(contract, "conId", None))
            portfolio_item = portfolio_by_con_id.get(con_id) if con_id is not None else None
            positions.append(
                {
                    "account_id": getattr(item, "account", None) or account_id or None,
                    "instrument": _contract_label(contract),
                    "symbol": getattr(contract, "symbol", None) or None,
                    "local_symbol": getattr(contract, "localSymbol", None) or None,
                    "con_id": con_id,
                    "quantity": float(item.position),
                    "avg_price": float(item.avgCost),
                    "market_value": (
                        float(portfolio_item.marketValue)
                        if portfolio_item is not None
                        else None
                    ),
                    "unrealized_pnl": (
                        float(portfolio_item.unrealizedPNL)
                        if portfolio_item is not None
                        else None
                    ),
                }
            )

        open_orders: list[dict[str, Any]] = []
        for trade in open_trades:
            order = getattr(trade, "order", None)
            contract = getattr(trade, "contract", None)
            order_status = getattr(trade, "orderStatus", None)
            if order is None or contract is None or order_status is None:
                continue

            trade_account = getattr(order, "account", None) or account_id or None
            if account_id and trade_account not in {account_id, None, ""}:
                continue

            open_orders.append(
                {
                    "account_id": trade_account,
                    "instrument": _contract_label(contract),
                    "symbol": getattr(contract, "symbol", None) or None,
                    "local_symbol": getattr(contract, "localSymbol", None) or None,
                    "con_id": _to_optional_int(getattr(contract, "conId", None)),
                    "status": getattr(order_status, "status", None) or None,
                    "side": getattr(order, "action", None) or None,
                    "order_ref": getattr(order, "orderRef", None) or None,
                    "model_code": getattr(order, "modelCode", None) or None,
                    "quantity": _to_float_or_zero(getattr(order, "totalQuantity", None)),
                    "remaining": _to_float_or_zero(getattr(order_status, "remaining", None)),
                    "order_id": _to_optional_int(getattr(order, "orderId", None)),
                    "perm_id": _to_optional_int(getattr(order, "permId", None)),
                    "client_id": _to_optional_int(getattr(order, "clientId", None)),
                }
            )

        return BrokerSnapshot(
            connected=bool(state.ib.isConnected()),
            mock_mode=False,
            generated_at=generated_at,
            positions=positions,
            open_orders=open_orders,
        )

    async def health(self, *, paper_trading: bool = True) -> dict[str, str | bool]:
        state = await self.connect(paper_trading=paper_trading)

        if state.mock_mode:
            return {"status": "degraded", "connected": False, "mock_mode": True}

        connected = bool(state.ib.isConnected())
        return {
            "status": "ok" if connected else "degraded",
            "connected": connected,
            "mock_mode": state.mock_mode,
        }


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    raw = getattr(value, "value", 0)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _to_float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_optional_int(value: Any) -> int | None:
    if value in (None, "", 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _contract_label(contract: Any) -> str:
    return str(
        getattr(contract, "localSymbol", None)
        or getattr(contract, "symbol", None)
        or getattr(contract, "conId", None)
        or "UNKNOWN"
    )


ib_account_service = IBAccountService()
