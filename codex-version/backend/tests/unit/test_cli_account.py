from __future__ import annotations

import json

from typer.testing import CliRunner

from msai import cli as cli_module
from msai.services.ib_account import AccountSummary, BrokerSnapshot

runner = CliRunner()


def test_account_summary_command_outputs_richer_margin_fields(monkeypatch) -> None:
    class _StubAccountService:
        async def summary(self, *, paper_trading: bool = True) -> AccountSummary:
            assert paper_trading is True
            return AccountSummary(
                net_liquidation=1_000_000.0,
                equity_with_loan_value=995_000.0,
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

    monkeypatch.setattr(cli_module, "ib_account_service", _StubAccountService())

    result = runner.invoke(cli_module.app, ["account", "summary"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["equity_with_loan_value"] == 995000.0
    assert payload["maintenance_margin_requirement"] == 110000.0
    assert payload["gross_position_value"] == 400000.0


def test_account_snapshot_command_outputs_positions_and_orders(monkeypatch) -> None:
    class _StubAccountService:
        async def reconciliation_snapshot(self, *, paper_trading: bool = True) -> BrokerSnapshot:
            assert paper_trading is True
            return BrokerSnapshot(
                connected=True,
                mock_mode=False,
                generated_at="2026-04-08T23:40:00Z",
                positions=[{"instrument": "EUR/USD.IDEALPRO", "quantity": 500.0}],
                open_orders=[{"instrument": "EUR/USD.IDEALPRO", "status": "Submitted"}],
            )

    monkeypatch.setattr(cli_module, "ib_account_service", _StubAccountService())

    result = runner.invoke(cli_module.app, ["account", "snapshot"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["connected"] is True
    assert payload["positions"][0]["instrument"] == "EUR/USD.IDEALPRO"
    assert payload["open_orders"][0]["status"] == "Submitted"
