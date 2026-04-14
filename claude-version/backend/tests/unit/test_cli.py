"""Unit tests for the ``msai`` CLI.

Verifies the command tree structure (sub-apps + sub-commands) and
exercises the HTTP-backed commands against a mocked httpx layer.  The
data-ingest commands invoke real services (ParquetStore backed by a
tempdir) so the top-level routing is end-to-end tested without a
running API server.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from msai.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ----------------------------------------------------------------------
# Command-tree structure
# ----------------------------------------------------------------------


class TestCommandTree:
    def test_root_help_lists_all_sub_apps(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for sub in (
            "strategy",
            "backtest",
            "research",
            "live",
            "graduation",
            "portfolio",
            "account",
            "system",
        ):
            assert sub in result.output

    @pytest.mark.parametrize(
        ("sub_app", "expected_commands"),
        [
            ("strategy", {"list", "show", "validate"}),
            ("backtest", {"run", "history", "show"}),
            ("research", {"list", "show", "cancel"}),
            ("live", {"start", "stop", "status", "kill-all"}),
            ("graduation", {"list", "show"}),
            ("portfolio", {"list", "runs", "show", "run"}),
            ("account", {"summary", "positions", "health"}),
            ("system", {"health"}),
        ],
    )
    def test_sub_app_lists_expected_commands(
        self,
        runner: CliRunner,
        sub_app: str,
        expected_commands: set[str],
    ) -> None:
        result = runner.invoke(app, [sub_app, "--help"])
        assert result.exit_code == 0
        for command in expected_commands:
            assert command in result.output


# ----------------------------------------------------------------------
# Auth headers + URL resolution
# ----------------------------------------------------------------------


class TestAuthAndUrl:
    def test_api_key_env_wins_over_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from msai.cli import _api_headers

        monkeypatch.setenv("MSAI_API_KEY", "env-override")
        headers = _api_headers()
        assert headers["X-API-Key"] == "env-override"

    def test_api_url_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from msai.cli import _api_base

        monkeypatch.setenv("MSAI_API_URL", "http://custom:9000")
        assert _api_base() == "http://custom:9000"


# ----------------------------------------------------------------------
# HTTP-backed commands — mock httpx.request so we exercise routing
# ----------------------------------------------------------------------


def _ok_response(body: dict[str, Any] | list[Any]) -> MagicMock:
    """Build a MagicMock httpx.Response equivalent for success cases."""
    response = MagicMock(spec=httpx.Response)
    response.is_success = True
    response.status_code = 200
    response.json.return_value = body
    response.text = json.dumps(body)
    return response


class TestHttpCommands:
    def test_strategy_list_calls_correct_endpoint(self, runner: CliRunner) -> None:
        body = {"items": [{"id": "s-1", "name": "EMA"}], "total": 1}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["strategy", "list"])
        assert result.exit_code == 0
        assert mock.called
        args, kwargs = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/strategies/" in args[1]
        assert "EMA" in result.output

    def test_backtest_run_posts_expected_payload(self, runner: CliRunner) -> None:
        body = {"id": "bt-42", "status": "pending"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(
                app,
                [
                    "backtest",
                    "run",
                    "strategy-uuid",
                    "AAPL,SPY",
                    "2024-01-01",
                    "2024-06-01",
                    "--config-json",
                    '{"fast": 5}',
                ],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["json"] == {
            "strategy_id": "strategy-uuid",
            "config": {"fast": 5},
            "instruments": ["AAPL", "SPY"],
            "start_date": "2024-01-01",
            "end_date": "2024-06-01",
        }

    def test_backtest_run_rejects_invalid_json_config(self, runner: CliRunner) -> None:
        result = runner.invoke(
            app,
            [
                "backtest",
                "run",
                "sid",
                "AAPL",
                "2024-01-01",
                "2024-02-01",
                "--config-json",
                "not-json",
            ],
        )
        assert result.exit_code != 0
        assert "invalid" in result.output.lower()

    def test_portfolio_run_posts_dates(self, runner: CliRunner) -> None:
        body = {"id": "run-9", "status": "pending"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(
                app,
                [
                    "portfolio",
                    "run",
                    "pid-123",
                    "2024-01-01",
                    "2025-01-01",
                    "--max-parallelism",
                    "4",
                ],
            )
        assert result.exit_code == 0
        args, kwargs = mock.call_args
        assert args[0] == "POST"
        assert "/api/v1/portfolios/pid-123/runs" in args[1]
        assert kwargs["json"] == {
            "start_date": "2024-01-01",
            "end_date": "2025-01-01",
            "max_parallelism": 4,
        }

    def test_live_kill_all_requires_confirmation(self, runner: CliRunner) -> None:
        # Without --yes, Typer's confirm prompt aborts when user declines.
        with patch("msai.cli.httpx.request") as mock:
            result = runner.invoke(app, ["live", "kill-all"], input="n\n")
        assert result.exit_code != 0
        assert mock.call_count == 0  # must not hit the API

    def test_live_kill_all_with_yes_skips_prompt(self, runner: CliRunner) -> None:
        body = {"stopped": 3, "risk_halted": True}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["live", "kill-all", "--yes"])
        assert result.exit_code == 0
        assert mock.called
        assert "Stopped 3" in result.output

    def test_graduation_list_passes_stage_filter(self, runner: CliRunner) -> None:
        body = {"items": [], "total": 0}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["graduation", "list", "--stage", "promoted"])
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["params"]["stage"] == "promoted"

    def test_connection_error_surfaces_clear_message(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request",
            side_effect=httpx.ConnectError("refused"),
        ):
            result = runner.invoke(app, ["strategy", "list"])
        assert result.exit_code != 0
        assert "Connection refused" in result.output

    def test_non_2xx_surfaces_body_in_error(self, runner: CliRunner) -> None:
        error_response = MagicMock(spec=httpx.Response)
        error_response.is_success = False
        error_response.status_code = 500
        error_response.text = "oops"
        with patch("msai.cli.httpx.request", return_value=error_response):
            result = runner.invoke(app, ["strategy", "list"])
        assert result.exit_code != 0
        assert "500" in result.output
        assert "oops" in result.output


# ----------------------------------------------------------------------
# Ingest commands — direct service invocation, no HTTP
# ----------------------------------------------------------------------


class TestIngestCommands:
    def test_ingest_rejects_empty_symbols(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["ingest", "stocks", "   ", "2024-01-01", "2024-06-01"])
        assert result.exit_code != 0
        assert "no symbols" in result.output.lower()

    def test_ingest_daily_rejects_unknown_all_symbols(
        self, runner: CliRunner, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point parquet_root at an empty tmpdir so list_symbols returns [].
        import msai.core.config as config_module

        monkeypatch.setattr(config_module.settings, "data_root", tmp_path, raising=True)
        result = runner.invoke(app, ["ingest-daily", "stocks", "all"])
        assert result.exit_code != 0
        assert "no existing symbols" in result.output.lower()
