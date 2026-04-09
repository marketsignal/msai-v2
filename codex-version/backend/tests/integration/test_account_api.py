from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from msai.api import account as account_api
from msai.core.config import settings
from msai.main import app
from msai.services.ib_account import AccountSummary, BrokerSnapshot


@pytest.fixture(autouse=True)
def _reset_account_api_state() -> None:
    app.dependency_overrides.clear()


def test_account_endpoints_return_summary_snapshot_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")

    class _StubAccountService:
        async def summary(self, *, paper_trading: bool = True) -> AccountSummary:
            assert paper_trading is True
            return AccountSummary(
                net_liquidation=1_000_000.0,
                equity_with_loan_value=990_000.0,
                buying_power=2_000_000.0,
                margin_used=125_000.0,
                initial_margin_requirement=125_000.0,
                maintenance_margin_requirement=110_000.0,
                available_funds=875_000.0,
                excess_liquidity=860_000.0,
                sma=50_000.0,
                gross_position_value=400_000.0,
                cushion=0.86,
                unrealized_pnl=1_250.0,
            )

        async def portfolio(self, *, paper_trading: bool = True) -> list[dict[str, float | str]]:
            assert paper_trading is True
            return [
                {
                    "instrument": "EUR.USD",
                    "quantity": 500.0,
                    "avg_price": 1.1664,
                    "market_value": 583.2,
                    "unrealized_pnl": 1.5,
                }
            ]

        async def reconciliation_snapshot(self, *, paper_trading: bool = True) -> BrokerSnapshot:
            assert paper_trading is True
            return BrokerSnapshot(
                connected=True,
                mock_mode=False,
                generated_at="2026-04-08T23:40:00Z",
                positions=[
                    {
                        "account_id": "DUP733211",
                        "instrument": "EUR/USD.IDEALPRO",
                        "quantity": 500.0,
                    }
                ],
                open_orders=[
                    {
                        "account_id": "DUP733211",
                        "instrument": "EUR/USD.IDEALPRO",
                        "status": "Submitted",
                    }
                ],
            )

        async def health(self, *, paper_trading: bool = True) -> dict[str, str | bool]:
            assert paper_trading is True
            return {"status": "ok", "connected": True, "mock_mode": False}

    monkeypatch.setattr(account_api, "ib_account_service", _StubAccountService())
    monkeypatch.setattr(
        account_api,
        "ib_probe",
        SimpleNamespace(health_status=lambda: {"status": "healthy", "consecutive_failures": 0}),
    )

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}
        summary_response = client.get("/api/v1/account/summary", headers=headers)
        portfolio_response = client.get("/api/v1/account/portfolio", headers=headers)
        snapshot_response = client.get("/api/v1/account/snapshot", headers=headers)
        health_response = client.get("/api/v1/account/health", headers=headers)

    assert summary_response.status_code == 200
    assert summary_response.json()["equity_with_loan_value"] == 990000.0
    assert summary_response.json()["maintenance_margin_requirement"] == 110000.0
    assert summary_response.json()["cushion"] == 0.86

    assert portfolio_response.status_code == 200
    assert portfolio_response.json()[0]["instrument"] == "EUR.USD"

    assert snapshot_response.status_code == 200
    assert snapshot_response.json()["open_orders"][0]["status"] == "Submitted"
    assert snapshot_response.json()["positions"][0]["account_id"] == "DUP733211"

    assert health_response.status_code == 200
    assert health_response.json()["paper_trading"] is True
    assert health_response.json()["connected"] is True
