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


def _api_base_for_test() -> str:
    """Default base URL the CLI assembles when no env override is set."""
    return "http://localhost:8000"


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

    def test_backtest_history_uses_page_params(self, runner: CliRunner) -> None:
        # The backend endpoint paginates via ``page`` / ``page_size`` —
        # ``limit`` is silently ignored.  Regression guard: keep the
        # CLI param names aligned with the server contract.
        with patch("msai.cli.httpx.request", return_value=_ok_response({"items": []})) as mock:
            result = runner.invoke(app, ["backtest", "history", "--page", "2", "--page-size", "50"])
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["params"] == {"page": 2, "page_size": 50}

    def test_research_list_uses_page_params(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request", return_value=_ok_response({"items": []})) as mock:
            result = runner.invoke(app, ["research", "list", "--page", "3", "--page-size", "10"])
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["params"] == {"page": 3, "page_size": 10}

    def test_url_encoding_prevents_path_injection(self, runner: CliRunner) -> None:
        # A hostile strategy-id containing "../" would otherwise let the
        # authenticated CLI request a different endpoint (``httpx``
        # normalizes paths).  Verify ``_url_id`` percent-encodes so the
        # original segment is preserved as a path component.
        with patch("msai.cli.httpx.request", return_value=_ok_response({"ok": True})) as mock:
            result = runner.invoke(app, ["strategy", "show", "../account/summary"])
        assert result.exit_code == 0
        url = mock.call_args[0][1]
        # `..` must be percent-encoded; the request must hit
        # /api/v1/strategies/..%2F... not /api/v1/account/summary.
        assert "%2F" in url
        # The encoded id must stay inside /api/v1/strategies/ with NO
        # raw `/` separators after "strategies/".  If there were, httpx
        # would resolve the extra segment and the call would hit a
        # different route.
        tail = url.split("/api/v1/strategies/", 1)[1]
        assert "/" not in tail

    def test_graduation_list_passes_stage_filter(self, runner: CliRunner) -> None:
        body = {"items": [], "total": 0}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["graduation", "list", "--stage", "live_candidate"])
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["params"]["stage"] == "live_candidate"

    def test_graduation_show_merges_candidate_and_transitions(self, runner: CliRunner) -> None:
        # ``show`` promises the transition audit trail — verify both
        # endpoints are called and the outputs are merged.
        candidate_response = _ok_response({"id": "c-1", "stage": "live_candidate"})
        transitions_response = _ok_response(
            [{"from_stage": "paper_review", "to_stage": "live_candidate"}]
        )
        with (
            patch("msai.cli.httpx.request", return_value=candidate_response) as req_mock,
            patch("msai.cli.httpx.get", return_value=transitions_response) as get_mock,
        ):
            result = runner.invoke(app, ["graduation", "show", "c-1"])
        assert result.exit_code == 0
        assert req_mock.call_count == 1
        assert get_mock.call_count == 1
        assert "/candidates/c-1/transitions" in get_mock.call_args[0][0]
        assert '"candidate"' in result.output
        assert '"transitions"' in result.output

    def test_system_health_treats_unhealthy_ib_body_as_not_ok(self, runner: CliRunner) -> None:
        # /api/v1/account/health returns 200 even when IB is down,
        # with {"status": "unhealthy", "gateway_connected": false}.
        # Regression guard: system health must NOT report account ok
        # in that case, or the command defeats its own purpose.
        def _mock_get(url, **_kwargs):
            if "/account/health" in url:
                return _ok_response({"status": "unhealthy", "gateway_connected": False})
            return _ok_response({"status": "ok"})

        with patch("msai.cli.httpx.get", side_effect=_mock_get):
            result = runner.invoke(app, ["system", "health"])
        assert result.exit_code == 0
        # Parse the JSON output — must have "ok": false for account,
        # true for api/ready/live.
        output_json = json.loads(result.output)
        assert output_json["account"]["ok"] is False
        assert output_json["api"]["ok"] is True

    def test_connection_error_surfaces_clear_message(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request",
            side_effect=httpx.ConnectError("refused"),
        ):
            result = runner.invoke(app, ["strategy", "list"])
        assert result.exit_code != 0
        assert "Connection refused" in result.output

    def test_read_timeout_surfaces_clear_message(self, runner: CliRunner) -> None:
        # Regression: before the fix, ReadTimeout on live-start (slow IB
        # connection) leaked a raw httpx traceback to stderr.  Now it
        # should land in the TimeoutException branch of _api_call.
        with patch(
            "msai.cli.httpx.request",
            side_effect=httpx.ReadTimeout("slow"),
        ):
            # Codex iter-3 P2: live start now requires --ib-login-key.
            # PR #67 Codex P2: live start now also enforces the
            # account/paper prefix guard before HTTP — must use a DU*
            # paper-prefix account (or U* with --no-paper) for the
            # timeout-surfacing path to actually reach httpx.request.
            result = runner.invoke(
                app,
                ["live", "start", "sid", "DU1234567", "--ib-login-key", "k"],
            )
        assert result.exit_code != 0
        assert "timed out" in result.output.lower()

    def test_generic_request_error_surfaces_type(self, runner: CliRunner) -> None:
        # `NetworkError` is a concrete `RequestError` subclass — covers
        # DNS failures, TLS handshake breakdowns, etc. that aren't
        # ConnectError or TimeoutException.
        with patch(
            "msai.cli.httpx.request",
            side_effect=httpx.NetworkError("tls handshake failed"),
        ):
            result = runner.invoke(app, ["strategy", "list"])
        assert result.exit_code != 0
        assert "Request failed" in result.output
        assert "NetworkError" in result.output

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
