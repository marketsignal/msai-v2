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


def test_readiness_passes_provider_query_param() -> None:
    """`msai symbols readiness --provider interactive_brokers` must forward the
    provider as a query param so an operator can pin a dual-provider symbol's view."""
    runner = CliRunner()
    fake_response = {
        "instrument_uid": "abc",
        "registered": True,
        "provider": "interactive_brokers",
        "backtest_data_available": None,
        "coverage_status": None,
        "covered_range": None,
        "missing_ranges": [],
        "live_qualified": True,
        "coverage_summary": "…",
    }
    with patch("msai.cli._api_call") as api_mock:
        api_mock.return_value.json.return_value = fake_response
        result = runner.invoke(
            symbols_app,
            [
                "readiness",
                "--symbol",
                "SPY",
                "--asset-class",
                "equity",
                "--provider",
                "interactive_brokers",
            ],
        )
    assert result.exit_code == 0, result.stdout
    _, kwargs = api_mock.call_args
    assert kwargs["params"]["provider"] == "interactive_brokers"
    assert kwargs["params"]["symbol"] == "SPY"
    assert kwargs["params"]["asset_class"] == "equity"


def test_readiness_omits_provider_when_unpinned() -> None:
    """Without --provider, no provider key is sent (server applies its default
    preference policy)."""
    runner = CliRunner()
    fake_response = {
        "instrument_uid": "abc",
        "registered": True,
        "provider": "databento",
        "backtest_data_available": None,
        "coverage_status": None,
        "covered_range": None,
        "missing_ranges": [],
        "live_qualified": True,
        "coverage_summary": "…",
    }
    with patch("msai.cli._api_call") as api_mock:
        api_mock.return_value.json.return_value = fake_response
        result = runner.invoke(
            symbols_app,
            ["readiness", "--symbol", "SPY", "--asset-class", "equity"],
        )
    assert result.exit_code == 0, result.stdout
    _, kwargs = api_mock.call_args
    assert "provider" not in kwargs["params"]


def test_readiness_rejects_unknown_provider() -> None:
    """An out-of-enum provider is rejected by Typer before any API call."""
    runner = CliRunner()
    with patch("msai.cli._api_call") as api_mock:
        result = runner.invoke(
            symbols_app,
            [
                "readiness",
                "--symbol",
                "SPY",
                "--asset-class",
                "equity",
                "--provider",
                "bogus",
            ],
        )
    assert result.exit_code != 0
    api_mock.assert_not_called()


def test_delete_passes_provider_query_param() -> None:
    """`msai symbols delete SPY --provider databento` forwards the provider so an
    operator can satisfy the destructive-op disambiguation requirement."""
    runner = CliRunner()
    with patch("msai.cli._api_call") as api_mock:
        # 204 success path returns no body; CLI must not call .json().
        result = runner.invoke(
            symbols_app,
            [
                "delete",
                "SPY",
                "--asset-class",
                "equity",
                "--provider",
                "databento",
                "--yes",
            ],
        )
    assert result.exit_code == 0, result.stdout
    _, kwargs = api_mock.call_args
    assert kwargs["params"]["provider"] == "databento"
    assert kwargs["params"]["asset_class"] == "equity"


def test_delete_omits_provider_when_unpinned() -> None:
    """Without --provider, no provider key is sent (server may 422 for a
    dual-provider symbol — that error is what surfaces to the operator)."""
    runner = CliRunner()
    with patch("msai.cli._api_call") as api_mock:
        result = runner.invoke(
            symbols_app,
            ["delete", "SPY", "--asset-class", "equity", "--yes"],
        )
    assert result.exit_code == 0, result.stdout
    _, kwargs = api_mock.call_args
    assert "provider" not in kwargs["params"]


def test_delete_surfaces_ambiguous_422_message() -> None:
    """When the API 422s an unpinned dual-provider delete, the now-satisfiable
    AMBIGUOUS_INSTRUMENT message must reach the operator (stderr) so they know to
    re-run with --provider."""
    import typer

    from msai.cli import _fail

    runner = CliRunner()
    ambiguous_text = (
        '{"error":{"code":"AMBIGUOUS_INSTRUMENT","message":'
        "\"Symbol 'SPY' (asset_class='equity') matches definitions under multiple "
        "providers (['databento', 'interactive_brokers']); pin provider explicitly "
        'via the provider query param."}}'
    )

    def _raise_api_error(*_args: object, **_kwargs: object) -> None:
        # Mirror _api_call's real non-2xx behavior: render the API error body
        # via _fail (stderr) then raise typer.Exit.
        _fail(f"API error (422): {ambiguous_text}")
        raise typer.Exit(code=1)

    with patch("msai.cli._api_call", side_effect=_raise_api_error):
        result = runner.invoke(
            symbols_app,
            ["delete", "SPY", "--asset-class", "equity", "--yes"],
        )
    assert result.exit_code != 0
    # CliRunner mixes stderr into output by default; _fail writes to stderr.
    assert "AMBIGUOUS_INSTRUMENT" in result.output
    assert "provider" in result.output.lower()
