import pytest

from msai.core.config import settings
from msai.services.ib_account import IBAccountService


class FailingIB:
    def __init__(self) -> None:
        self._connected = False

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    async def connectAsync(self, **kwargs) -> None:  # noqa: N802
        _ = kwargs
        raise RuntimeError("connection failed")


class ConnectedIB:
    def __init__(self) -> None:
        self._connected = True
        self.last_connect_kwargs: dict | None = None

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    async def connectAsync(self, **kwargs) -> None:  # noqa: N802
        self.last_connect_kwargs = kwargs
        self._connected = True

    async def accountSummaryAsync(self, account: str = "") -> list[object]:  # noqa: N802
        _ = account

        class Row:
            def __init__(self, tag: str, value: str) -> None:
                self.tag = tag
                self.value = value

        return [
            Row("NetLiquidation", "1000"),
            Row("EquityWithLoanValue", "980"),
            Row("BuyingPower", "2000"),
            Row("AvailableFunds", "900"),
            Row("ExcessLiquidity", "850"),
            Row("UnrealizedPnL", "10"),
            Row("InitMarginReq", "125"),
            Row("MaintMarginReq", "110"),
            Row("SMA", "500"),
            Row("GrossPositionValue", "750"),
            Row("Cushion", "0.85"),
        ]

    def portfolio(self, account: str = "") -> list[object]:
        _ = account
        return []

    async def reqPositionsAsync(self) -> list[object]:  # noqa: N802
        return self.positions()

    async def reqAllOpenOrdersAsync(self) -> list[object]:  # noqa: N802
        return []

    def positions(self, account: str = "") -> list[object]:
        _ = account
        return []


@pytest.mark.asyncio
async def test_ib_account_fallback_to_mock() -> None:
    service = IBAccountService()
    service._states[True].ib = FailingIB()  # type: ignore[assignment]

    summary = await service.summary()

    assert summary.net_liquidation > 0
    assert service._states[True].mock_mode is True


@pytest.mark.asyncio
async def test_ib_account_real_summary_parsing() -> None:
    service = IBAccountService()
    client = ConnectedIB()
    service._states[True].ib = client  # type: ignore[assignment]

    summary = await service.summary()

    assert summary.net_liquidation == 1000.0
    assert summary.equity_with_loan_value == 980.0
    assert summary.buying_power == 2000.0
    assert summary.margin_used == 125.0
    assert summary.initial_margin_requirement == 125.0
    assert summary.maintenance_margin_requirement == 110.0
    assert summary.excess_liquidity == 850.0
    assert summary.sma == 500.0
    assert summary.gross_position_value == 750.0
    assert summary.cushion == 0.85
    assert client.last_connect_kwargs is None


@pytest.mark.asyncio
async def test_ib_account_live_summary_uses_live_gateway_port() -> None:
    service = IBAccountService()
    client = ConnectedIB()
    client._connected = False
    service._states[False].ib = client  # type: ignore[assignment]

    await service.summary(paper_trading=False)

    assert client.last_connect_kwargs is not None
    assert client.last_connect_kwargs["port"] == settings.ib_gateway_port_live


@pytest.mark.asyncio
async def test_ib_account_reconciliation_snapshot_includes_open_order_client_id() -> None:
    service = IBAccountService()

    class PositionItem:
        def __init__(self) -> None:
            self.account = "DU123456"
            self.contract = type("Contract", (), {"symbol": "AAPL", "localSymbol": "AAPL", "conId": 1})()
            self.position = 5
            self.avgCost = 200.0

    class PortfolioItem:
        def __init__(self) -> None:
            self.contract = type("Contract", (), {"conId": 1})()
            self.marketValue = 1000.0
            self.unrealizedPNL = 10.0

    class TradeItem:
        def __init__(self) -> None:
            self.contract = type("Contract", (), {"symbol": "AAPL", "localSymbol": "AAPL", "conId": 1})()
            self.order = type(
                "Order",
                (),
                {"account": "DU123456", "action": "BUY", "orderRef": "ref-1", "modelCode": "", "totalQuantity": 5, "orderId": 7, "permId": 70, "clientId": 22},
            )()
            self.orderStatus = type("OrderStatus", (), {"status": "Submitted", "remaining": 5})()

    client = ConnectedIB()

    async def _req_all_open_orders() -> list[object]:
        return [TradeItem()]

    client.positions = lambda account="": [PositionItem()]  # type: ignore[method-assign]
    client.portfolio = lambda account="": [PortfolioItem()]  # type: ignore[method-assign]
    client.reqAllOpenOrdersAsync = _req_all_open_orders  # type: ignore[method-assign]
    service._states[True].ib = client  # type: ignore[assignment]

    snapshot = await service.reconciliation_snapshot()

    assert snapshot.positions[0]["instrument"] == "AAPL"
    assert snapshot.open_orders[0]["client_id"] == 22
    assert snapshot.open_orders[0]["order_ref"] == "ref-1"
