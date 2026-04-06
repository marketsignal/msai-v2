import pytest

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

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    async def connectAsync(self, **kwargs) -> None:  # noqa: N802
        _ = kwargs
        self._connected = True

    async def accountSummaryAsync(self, account: str = "") -> list[object]:  # noqa: N802
        _ = account

        class Row:
            def __init__(self, tag: str, value: str) -> None:
                self.tag = tag
                self.value = value

        return [
            Row("NetLiquidation", "1000"),
            Row("BuyingPower", "2000"),
            Row("AvailableFunds", "900"),
            Row("UnrealizedPnL", "10"),
        ]

    def portfolio(self, account: str = "") -> list[object]:
        _ = account
        return []


@pytest.mark.asyncio
async def test_ib_account_fallback_to_mock() -> None:
    service = IBAccountService()
    service._ib = FailingIB()  # type: ignore[assignment]

    summary = await service.summary()

    assert summary.net_liquidation > 0
    assert service._mock_mode is True


@pytest.mark.asyncio
async def test_ib_account_real_summary_parsing() -> None:
    service = IBAccountService()
    service._ib = ConnectedIB()  # type: ignore[assignment]

    summary = await service.summary()

    assert summary.net_liquidation == 1000.0
    assert summary.buying_power == 2000.0
