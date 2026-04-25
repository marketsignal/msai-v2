from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from typer.testing import CliRunner

from msai.cli_symbols import app as symbols_app


def test_onboard_dry_run_prints_cost_summary(tmp_path: Path) -> None:
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        dedent(
            """
            watchlist_name: core
            symbols:
              - symbol: SPY
                asset_class: equity
                start: 2024-01-01
                end: 2024-12-31
            """
        )
    )
    runner = CliRunner()
    fake_response = {
        "watchlist_name": "core",
        "dry_run": True,
        "estimated_cost_usd": 0.42,
        "estimate_basis": "databento.metadata.get_cost (1m OHLCV)",
        "estimate_confidence": "high",
        "symbol_count": 1,
        "breakdown": [{"symbol": "SPY", "dataset": "XNAS.ITCH", "usd": 0.42}],
    }
    with patch("msai.cli._api_call") as api_mock:
        api_mock.return_value.json.return_value = fake_response
        result = runner.invoke(symbols_app, ["onboard", "--manifest", str(manifest), "--dry-run"])
    assert result.exit_code == 0
    assert "0.42" in result.stdout
    assert "high" in result.stdout


def test_status_exit_code_reflects_run_state(tmp_path: Path) -> None:
    runner = CliRunner()
    resp = {
        "run_id": "123e4567-e89b-12d3-a456-426614174000",
        "watchlist_name": "core",
        "status": "completed_with_failures",
        "progress": {"total": 2, "succeeded": 1, "failed": 1, "in_progress": 0, "not_started": 0},
        "per_symbol": [
            {
                "symbol": "SPY",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "succeeded",
                "step": "ib_skipped",
                "error": None,
                "next_action": None,
            },
            {
                "symbol": "AAPL",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "failed",
                "step": "ingest",
                "error": {"code": "INGEST_FAILED", "message": "rate limit"},
                "next_action": "Retry via /repair after checking Databento quota.",
            },
        ],
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
    }
    with patch("msai.cli._api_call") as api_mock:
        api_mock.return_value.json.return_value = resp
        result = runner.invoke(symbols_app, ["status", "123e4567-e89b-12d3-a456-426614174000"])
    assert result.exit_code == 1
    assert "AAPL" in result.stdout
    assert "INGEST_FAILED" in result.stdout


def test_cost_ceiling_usd_rejects_more_than_two_decimals(tmp_path: Path) -> None:
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        dedent(
            """
            watchlist_name: core
            symbols:
              - symbol: SPY
                asset_class: equity
                start: 2024-01-01
                end: 2024-12-31
            """
        )
    )
    runner = CliRunner()
    with patch("msai.cli._api_call") as api_mock:
        result = runner.invoke(
            symbols_app,
            ["onboard", "--manifest", str(manifest), "--cost-ceiling-usd", "123.456"],
        )
    assert result.exit_code != 0
    assert "2 decimal places" in result.stdout or "2 decimal places" in (result.stderr or "")
    api_mock.assert_not_called()


def test_cost_ceiling_usd_rejects_trailing_zero_overprecision(tmp_path: Path) -> None:
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        dedent(
            """
            watchlist_name: core
            symbols:
              - symbol: SPY
                asset_class: equity
                start: 2024-01-01
                end: 2024-12-31
            """
        )
    )
    runner = CliRunner()
    with patch("msai.cli._api_call") as api_mock:
        result = runner.invoke(
            symbols_app,
            ["onboard", "--manifest", str(manifest), "--cost-ceiling-usd", "123.450"],
        )
    assert result.exit_code != 0
    assert "2 decimal places" in result.stdout or "2 decimal places" in (result.stderr or "")
    api_mock.assert_not_called()


def test_cost_ceiling_usd_accepts_well_formed_decimal(tmp_path: Path) -> None:
    manifest = tmp_path / "m.yaml"
    manifest.write_text(
        dedent(
            """
            watchlist_name: core
            symbols:
              - symbol: SPY
                asset_class: equity
                start: 2024-01-01
                end: 2024-12-31
            """
        )
    )
    runner = CliRunner()
    fake_response = {"run_id": "abc", "watchlist_name": "core", "status": "pending"}
    with patch("msai.cli._api_call") as api_mock:
        api_mock.return_value.json.return_value = fake_response
        result = runner.invoke(
            symbols_app,
            ["onboard", "--manifest", str(manifest), "--cost-ceiling-usd", "123.45"],
        )
    assert result.exit_code == 0
    _, kwargs = api_mock.call_args
    assert kwargs["json_body"]["cost_ceiling_usd"] == "123.45"
