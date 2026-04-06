import pytest

from msai.services.alerting import AlertingService
from msai.services.ib_account import AccountSummary
from msai.services.ib_probe import IBProbe


class HealthyAccountService:
    async def health(self) -> dict[str, str | bool]:
        return {"status": "ok", "connected": True, "mock_mode": False}

    async def summary(self) -> AccountSummary:
        return AccountSummary(
            net_liquidation=1000.0,
            buying_power=2000.0,
            margin_used=100.0,
            available_funds=900.0,
            unrealized_pnl=10.0,
        )


@pytest.mark.asyncio
async def test_ib_probe_check_once() -> None:
    probe = IBProbe(HealthyAccountService(), AlertingService())
    ok = await probe.check_once()
    assert ok is True
    assert probe.health_status()["status"] == "healthy"
