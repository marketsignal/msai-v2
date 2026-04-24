"""CLI tests for `msai instruments bootstrap`. Mocks httpx.request so the
CLI path is exercised end-to-end without hitting a live server."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from msai.cli import app

runner = CliRunner()


def test_required_flags() -> None:
    """Missing --provider OR --symbols → Typer exits 2 (validation error)."""
    r = runner.invoke(app, ["instruments", "bootstrap"])
    assert r.exit_code == 2


def test_unsupported_provider_rejected() -> None:
    """--provider polygon → CLI exits non-zero with a helpful error."""
    r = runner.invoke(
        app,
        [
            "instruments",
            "bootstrap",
            "--provider",
            "polygon",
            "--symbols",
            "AAPL",
        ],
    )
    assert r.exit_code != 0


def test_success_prints_json_exits_0() -> None:
    """200 response → exit 0, stdout is valid JSON with summary."""
    with patch("httpx.request") as mock_req:
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "results": [
                    {
                        "symbol": "AAPL",
                        "outcome": "created",
                        "registered": True,
                        "backtest_data_available": None,
                        "live_qualified": False,
                        "canonical_id": "AAPL.NASDAQ",
                        "dataset": "XNAS.ITCH",
                        "asset_class": "equity",
                        "candidates": [],
                        "diagnostics": None,
                    }
                ],
                "summary": {"total": 1, "created": 1, "noop": 0, "alias_rotated": 0, "failed": 0},
            },
        )
        r = runner.invoke(
            app,
            [
                "instruments",
                "bootstrap",
                "--provider",
                "databento",
                "--symbols",
                "AAPL",
            ],
        )
    assert r.exit_code == 0
    # stdout has JSON (emit_json prints at end)
    # find the JSON block by scanning for '"summary"' in stdout
    assert '"summary"' in r.stdout
    assert '"created": 1' in r.stdout


def test_207_partial_exits_nonzero_but_prints_payload() -> None:
    """207 response (mixed success) → exit non-zero, but full payload printed."""
    with patch("httpx.request") as mock_req:
        mock_req.return_value = MagicMock(
            status_code=207,
            json=lambda: {
                "results": [
                    {
                        "symbol": "AAPL",
                        "outcome": "created",
                        "registered": True,
                        "backtest_data_available": None,
                        "live_qualified": False,
                        "canonical_id": "AAPL.NASDAQ",
                        "dataset": "XNAS.ITCH",
                        "asset_class": "equity",
                        "candidates": [],
                        "diagnostics": None,
                    },
                    {
                        "symbol": "BRK.B",
                        "outcome": "ambiguous",
                        "registered": False,
                        "backtest_data_available": False,
                        "live_qualified": False,
                        "canonical_id": None,
                        "dataset": "XNYS.PILLAR",
                        "asset_class": None,
                        "candidates": [
                            {
                                "alias_string": "BRK.B.XNYS",
                                "raw_symbol": "BRK.B",
                                "asset_class": "Equity",
                                "dataset": "XNYS.PILLAR",
                            }
                        ],
                        "diagnostics": None,
                    },
                ],
                "summary": {"total": 2, "created": 1, "noop": 0, "alias_rotated": 0, "failed": 1},
            },
        )
        r = runner.invoke(
            app,
            [
                "instruments",
                "bootstrap",
                "--provider",
                "databento",
                "--symbols",
                "AAPL,BRK.B",
            ],
        )
    assert r.exit_code != 0
    assert '"ambiguous"' in r.stdout


def test_exact_id_parses_alias_string() -> None:
    """--exact-id SYMBOL:ALIAS_STRING parses + is sent as dict in request body."""
    with patch("httpx.request") as mock_req:
        mock_req.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "results": [
                    {
                        "symbol": "BRK.B",
                        "outcome": "created",
                        "registered": True,
                        "backtest_data_available": None,
                        "live_qualified": False,
                        "canonical_id": "BRK.B.NYSE",
                        "dataset": "XNYS.PILLAR",
                        "asset_class": "equity",
                        "candidates": [],
                        "diagnostics": None,
                    }
                ],
                "summary": {"total": 1, "created": 1, "noop": 0, "alias_rotated": 0, "failed": 0},
            },
        )
        r = runner.invoke(
            app,
            [
                "instruments",
                "bootstrap",
                "--provider",
                "databento",
                "--symbols",
                "BRK.B",
                "--exact-id",
                "BRK.B:BRK.B.XNYS",
            ],
        )
    assert mock_req.call_count == 1
    sent_body = mock_req.call_args.kwargs["json"]
    assert sent_body["exact_ids"] == {"BRK.B": "BRK.B.XNYS"}
    assert r.exit_code == 0


def test_asset_class_enum_rejects_invalid_value() -> None:
    """--asset-class must be one of equity|futures|fx|option (registry taxonomy).
    'etf' and 'future' (singular) are rejected."""
    r = runner.invoke(
        app,
        [
            "instruments",
            "bootstrap",
            "--provider",
            "databento",
            "--symbols",
            "AAPL",
            "--asset-class",
            "etf",  # invalid (ETFs store as 'equity')
        ],
    )
    assert r.exit_code != 0
