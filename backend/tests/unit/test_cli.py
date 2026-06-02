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
        # Codex bot iter-5 P2 on PR #73: ``mode`` must be explicit in the
        # payload so the server doesn't inherit ``Portfolio.default_mode``
        # when the operator typed ``--mode quick``. Default is Quick.
        assert kwargs["json"] == {
            "start_date": "2024-01-01",
            "end_date": "2025-01-01",
            "max_parallelism": 4,
            "mode": "quick",
        }

    def test_portfolio_run_quick_mode_sends_explicit_mode(self, runner: CliRunner) -> None:
        """Codex bot iter-5 P2 on PR #73 — ``--mode quick`` MUST be sent
        in the payload so the server cannot inherit ``default_mode=full``
        from the parent portfolio and silently launch the Optuna walk-
        forward optimizer when the operator requested Quick.
        """
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
                    "--mode",
                    "quick",
                ],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["json"].get("mode") == "quick", kwargs["json"]

    def test_portfolio_run_full_mode_sends_explicit_mode(self, runner: CliRunner) -> None:
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
                    "--mode",
                    "full",
                    "--n-trials",
                    "10",
                ],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["json"].get("mode") == "full", kwargs["json"]
        assert kwargs["json"].get("n_trials") == 10, kwargs["json"]

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
# live status — supervisor (router) liveness rendering (PR 2 F4)
#
# The router heartbeat key has a 90s TTL, but the backend treats the
# supervisor as DEAD far earlier (the SPOF alert fires at 30s; the
# /start-portfolio gate at 15s). The CLI must NOT print "alive (45.0s ago)"
# for the 30-90s window — that hides an unmonitored fleet. It must warn
# "STALE/DOWN" once the age exceeds the shared SPOF threshold.
# ----------------------------------------------------------------------


class TestLiveStatusRouterHealth:
    @staticmethod
    def _status_body(router_age: float | None) -> dict[str, Any]:
        return {
            "risk_halted": False,
            "active_count": 0,
            "router_heartbeat_age_s": router_age,
            "deployments": [],
        }

    def test_fresh_router_age_renders_alive(self, runner: CliRunner) -> None:
        body = self._status_body(2.0)
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)):
            result = runner.invoke(app, ["live", "status"])
        assert result.exit_code == 0
        assert "alive" in result.output
        assert "2.0s ago" in result.output
        assert "STALE" not in result.output

    def test_stale_router_age_past_threshold_renders_stale_warning(self, runner: CliRunner) -> None:
        """A router age of 45s (> the 30s SPOF threshold but < the 90s TTL) is
        a DEAD supervisor that the old null-only check rendered as
        ``alive (45.0s ago)`` — hiding an unmonitored fleet. The CLI must warn
        STALE so the operator sees the fleet is unmonitored from the shell."""
        from msai.services.fleet_alerts import ROUTER_HEARTBEAT_SPOF_THRESHOLD_S

        stale_age = ROUTER_HEARTBEAT_SPOF_THRESHOLD_S + 15.0  # 45.0s by default
        body = self._status_body(stale_age)
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)):
            result = runner.invoke(app, ["live", "status"])
        assert result.exit_code == 0
        # The headline regression: must NOT claim the supervisor is alive.
        assert "alive" not in result.output
        assert "STALE" in result.output
        assert f"{stale_age:.1f}s ago" in result.output

    def test_missing_router_heartbeat_renders_down(self, runner: CliRunner) -> None:
        body = self._status_body(None)
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)):
            result = runner.invoke(app, ["live", "status"])
        assert result.exit_code == 0
        assert "DOWN" in result.output
        assert "alive" not in result.output


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


# ----------------------------------------------------------------------
# Smoke CLI — `msai backtest smoke` + `msai system smoke-alert`
# ----------------------------------------------------------------------


def _smoke_completed_body(config: str = "fast") -> dict[str, Any]:
    """Build a representative completed PortfolioRun response body."""
    return {
        "id": "00000000-0000-0000-0000-000000000111",
        "status": "completed",
        "smoke": True,
        "metrics": {
            "total_return": 0.05,
            "pnl": 5000.0,
            "sharpe": 1.3,
            "sortino": 1.6,
            "alpha": 0.02,
            "beta": 0.95,
            "max_drawdown": -0.08,
            "trade_count_by_strategy": {
                "__smoke__/smoke_market_order/AAPL": 1,
                "__smoke__/smoke_market_order/SPY": 1,
            },
            "trade_count_total": 2,
            "benchmark_symbol": "SPY",
            "smoke_config": config,
        },
        "report_path": "/app/data/reports/portfolio_run_x.html",
        "error_message": None,
    }


class TestBacktestSmokeCommand:
    """Coverage for ``msai backtest smoke`` (Task 8)."""

    def test_default_config_is_fast_and_prints_pass_table(self, runner: CliRunner) -> None:
        # Arrange — first call: POST /portfolios/smoke/runs?config=fast (201)
        # Subsequent call(s): GET /portfolios/runs/{id} -> terminal completed body.
        completed = _smoke_completed_body(config="fast")
        create_resp = _ok_response(completed)
        create_resp.status_code = 201
        get_resp = _ok_response(completed)
        with patch(
            "msai.cli.httpx.request",
            side_effect=[create_resp, get_resp],
        ) as mock:
            # Act
            result = runner.invoke(app, ["backtest", "smoke"])

        # Assert
        assert result.exit_code == 0, result.output
        # POST URL carries config=fast (default)
        post_args, _ = mock.call_args_list[0]
        assert post_args[0] == "POST"
        assert "/api/v1/portfolios/smoke/runs" in post_args[1]
        assert "config=fast" in post_args[1]
        # GET URL targets the run-detail route
        get_args, _ = mock.call_args_list[1]
        assert get_args[0] == "GET"
        assert "/api/v1/portfolios/runs/00000000-0000-0000-0000-000000000111" in get_args[1]
        # Human-readable table is printed and verdict is PASS
        assert "PASS" in result.output
        assert "Sharpe" in result.output
        assert "Total return" in result.output

    def test_nightly_config_threads_through_post_url(self, runner: CliRunner) -> None:
        # Arrange
        completed = _smoke_completed_body(config="nightly")
        create_resp = _ok_response(completed)
        create_resp.status_code = 201
        get_resp = _ok_response(completed)
        with patch(
            "msai.cli.httpx.request",
            side_effect=[create_resp, get_resp],
        ) as mock:
            # Act
            result = runner.invoke(app, ["backtest", "smoke", "--config", "nightly"])

        # Assert
        assert result.exit_code == 0, result.output
        post_args, _ = mock.call_args_list[0]
        assert "config=nightly" in post_args[1]

    def test_json_flag_emits_machine_readable_payload(self, runner: CliRunner) -> None:
        # Arrange
        completed = _smoke_completed_body()
        create_resp = _ok_response(completed)
        create_resp.status_code = 201
        get_resp = _ok_response(completed)
        with patch(
            "msai.cli.httpx.request",
            side_effect=[create_resp, get_resp],
        ):
            # Act
            result = runner.invoke(app, ["backtest", "smoke", "--json"])

        # Assert
        assert result.exit_code == 0, result.output
        # JSON-only output: parse stdout as a single JSON document.
        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert payload["metrics"]["trade_count_total"] == 2
        # structural_problems is always present (empty on success)
        assert payload["structural_problems"] == []

    def test_structural_fail_when_a_symbol_produces_zero_trades(self, runner: CliRunner) -> None:
        # Arrange — the exact prod incident shape: status="completed" with a
        # healthy total (440), but smoke_market_order/AAPL produced 0 while SPY
        # carried the volume. The per-instrument floor must catch AAPL's 0 even
        # though the total is well above the old SUM floor of 2.
        completed = _smoke_completed_body()
        completed["metrics"]["trade_count_by_strategy"] = {
            "__smoke__/ema_cross/AAPL": 0,
            "__smoke__/ema_cross/SPY": 438,
            "__smoke__/smoke_market_order/AAPL": 0,
            "__smoke__/smoke_market_order/SPY": 2,
        }
        completed["metrics"]["trade_count_total"] = 440
        create_resp = _ok_response(completed)
        create_resp.status_code = 201
        get_resp = _ok_response(completed)
        with patch(
            "msai.cli.httpx.request",
            side_effect=[create_resp, get_resp],
        ):
            # Act
            result = runner.invoke(app, ["backtest", "smoke"])

        # Assert — structural FAIL naming the offending per-instrument strategy.
        assert result.exit_code == 1, result.output
        assert "FAIL" in result.output
        assert "__smoke__/smoke_market_order/AAPL" in result.output

    def test_structural_fail_when_a_smoke_market_order_key_is_absent(
        self, runner: CliRunner
    ) -> None:
        # Arrange — AAPL's smoke_market_order key is MISSING entirely (not just
        # 0). The floor asserts the EXPECTED keys from the config, so an absent
        # key must also fail (Codex plan-review P1).
        completed = _smoke_completed_body()
        completed["metrics"]["trade_count_by_strategy"] = {
            "__smoke__/smoke_market_order/SPY": 2,
        }
        completed["metrics"]["trade_count_total"] = 2
        create_resp = _ok_response(completed)
        create_resp.status_code = 201
        get_resp = _ok_response(completed)
        with patch(
            "msai.cli.httpx.request",
            side_effect=[create_resp, get_resp],
        ):
            result = runner.invoke(app, ["backtest", "smoke"])

        assert result.exit_code == 1, result.output
        assert "__smoke__/smoke_market_order/AAPL" in result.output

    def test_structural_fail_in_json_mode_writes_problems_and_exits_nonzero(
        self, runner: CliRunner
    ) -> None:
        # Arrange — missing G5 keys
        completed = _smoke_completed_body()
        completed["metrics"].pop("alpha")
        completed["metrics"].pop("beta")
        create_resp = _ok_response(completed)
        create_resp.status_code = 201
        get_resp = _ok_response(completed)
        with patch(
            "msai.cli.httpx.request",
            side_effect=[create_resp, get_resp],
        ):
            # Act
            result = runner.invoke(app, ["backtest", "smoke", "--json"])

        # Assert — JSON still emitted, exit code 1, problems listed
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        problems = payload["structural_problems"]
        assert problems, payload
        assert any("missing G5 keys" in p for p in problems)

    def test_rejects_unknown_config(self, runner: CliRunner) -> None:
        # Arrange — no HTTP should fire
        with patch("msai.cli.httpx.request") as mock:
            # Act
            result = runner.invoke(app, ["backtest", "smoke", "--config", "weekly"])

        # Assert
        assert result.exit_code == 2
        assert "unknown --config" in result.output.lower()
        assert mock.call_count == 0

    def test_json_mode_poll_timeout_emits_failure_json_on_stdout(self, runner: CliRunner) -> None:
        """In --json mode, a poll-deadline timeout still emits a JSON doc.

        Codex code-review P2: the poll-timeout branch used to write only to
        stderr, breaking the JSON contract automation parses from stdout.
        The branch must emit ``{"status": "timeout", ...}`` on stdout before
        exiting non-zero.
        """
        # Arrange — create returns a pending run; the poll GET keeps
        # returning a non-terminal status. Force the deadline to elapse on
        # the FIRST loop check by stubbing time.monotonic: the create-time
        # baseline is read once before the loop, then the first in-loop
        # check sees a value past the deadline.
        from msai.cli import _SMOKE_POLL_DEADLINE_SECONDS

        pending = {
            "id": "00000000-0000-0000-0000-000000000111",
            "status": "pending",
            "smoke": True,
            "metrics": None,
            "report_path": None,
            "error_message": None,
        }
        create_resp = _ok_response(pending)
        create_resp.status_code = 201
        get_resp = _ok_response(pending)
        with (
            patch(
                "msai.cli.httpx.request",
                side_effect=[create_resp, get_resp, get_resp, get_resp],
            ),
            # First call: baseline timeout_at = 0 + deadline. Second call
            # (the in-loop check) returns a value past that deadline.
            patch(
                "msai.cli.time.monotonic",
                side_effect=[0.0, _SMOKE_POLL_DEADLINE_SECONDS + 1.0],
            ),
        ):
            # Act
            result = runner.invoke(app, ["backtest", "smoke", "--json"])

        # Assert — non-zero exit AND a well-formed JSON document on stdout.
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "timeout"
        assert payload["id"] == "00000000-0000-0000-0000-000000000111"
        assert payload["metrics"] is None
        assert payload["structural_problems"] == ["poll deadline exceeded; run still in flight"]


class TestSystemSmokeAlertCommand:
    """Coverage for ``msai system smoke-alert`` (Task 8)."""

    def test_pass_dispatches_info_level(self, runner: CliRunner, tmp_path) -> None:
        # Arrange — write a clean PASS result file (no structural_problems)
        result_file = tmp_path / "smoke-result.json"
        body = _smoke_completed_body()
        body["structural_problems"] = []
        result_file.write_text(json.dumps(body))

        # Act
        with patch("msai.services.alerting.AlertingService.send_alert") as mock:
            invocation = runner.invoke(app, ["system", "smoke-alert", str(result_file)])

        # Assert
        assert invocation.exit_code == 0, invocation.output
        assert mock.call_count == 1
        _, kwargs = mock.call_args
        # Plan code uses keyword args — level/title/message.
        assert kwargs["level"] == "info"
        assert "PASS" in kwargs["title"]

    def test_structural_fail_dispatches_error_level(self, runner: CliRunner, tmp_path) -> None:
        # Arrange — completed status but structural_problems present
        result_file = tmp_path / "smoke-result.json"
        body = _smoke_completed_body()
        body["structural_problems"] = [
            "__smoke__/smoke_market_order/AAPL produced 0 trades; smoke_market_order must emit >=1 per instrument"
        ]
        result_file.write_text(json.dumps(body))

        # Act
        with patch("msai.services.alerting.AlertingService.send_alert") as mock:
            invocation = runner.invoke(app, ["system", "smoke-alert", str(result_file)])

        # Assert
        assert invocation.exit_code == 0, invocation.output
        _, kwargs = mock.call_args
        assert kwargs["level"] == "error"
        assert "FAIL" in kwargs["title"]
        # The message JSON body must include the structural problems list.
        message_body = json.loads(kwargs["message"])
        assert message_body["structural_problems"] == [
            "__smoke__/smoke_market_order/AAPL produced 0 trades; smoke_market_order must emit >=1 per instrument"
        ]

    def test_lifecycle_failed_status_dispatches_error_level(
        self, runner: CliRunner, tmp_path
    ) -> None:
        # Arrange — lifecycle status='failed' (no structural_problems list needed)
        result_file = tmp_path / "smoke-result.json"
        body = {
            "id": "x",
            "status": "failed",
            "smoke": True,
            "metrics": {},
            "report_path": "",
            "error_message": "ingest crashed",
            "structural_problems": [
                "status='failed': ingest crashed",
            ],
        }
        result_file.write_text(json.dumps(body))

        # Act
        with patch("msai.services.alerting.AlertingService.send_alert") as mock:
            invocation = runner.invoke(app, ["system", "smoke-alert", str(result_file)])

        # Assert
        assert invocation.exit_code == 0, invocation.output
        _, kwargs = mock.call_args
        assert kwargs["level"] == "error"

    # -----------------------------------------------------------------
    # Silent-failure iter-1 fix #2 — corrupt / missing result file MUST
    # still dispatch a level=error alert with a synthetic failure body
    # (NOT raise JSONDecodeError that the workflow's `|| echo` would
    # swallow into a silent warning).
    # -----------------------------------------------------------------

    def test_corrupt_json_in_result_file_synthesizes_failure_alert(
        self, runner: CliRunner, tmp_path
    ) -> None:
        # Arrange — non-JSON content (e.g., a Python traceback that landed
        # in the file before 2>${RESULT}.stderr was added).
        result_file = tmp_path / "smoke-result.json"
        result_file.write_text(
            "Traceback (most recent call last):\n"
            '  File "/app/...", line 1, in <module>\n'
            "    raise RuntimeError('something blew up')\n"
            "RuntimeError: something blew up\n"
        )

        # Act
        with patch("msai.services.alerting.AlertingService.send_alert") as mock:
            invocation = runner.invoke(app, ["system", "smoke-alert", str(result_file)])

        # Assert
        assert invocation.exit_code == 0, invocation.output
        assert mock.call_count == 1, "alert must dispatch even when JSON is corrupt"
        _, kwargs = mock.call_args
        assert kwargs["level"] == "error"
        assert "corrupt" in kwargs["title"].lower()
        # The synthesized body must mention the failure mode so the CI/
        # operator can diagnose without SSHing into the VM.
        message_body = json.loads(kwargs["message"])
        problems = message_body.get("structural_problems") or []
        assert any("corrupt" in p.lower() for p in problems), problems

    def test_missing_result_file_synthesizes_failure_alert(
        self, runner: CliRunner, tmp_path
    ) -> None:
        # Arrange — path that doesn't exist (OSError on read_text).
        result_file = tmp_path / "does-not-exist.json"

        # Act
        with patch("msai.services.alerting.AlertingService.send_alert") as mock:
            invocation = runner.invoke(app, ["system", "smoke-alert", str(result_file)])

        # Assert
        assert invocation.exit_code == 0, invocation.output
        assert mock.call_count == 1
        _, kwargs = mock.call_args
        assert kwargs["level"] == "error"

    def test_alerting_backend_failure_exits_with_code_2(self, runner: CliRunner, tmp_path) -> None:
        """If ``send_alert`` itself raises, the CLI must exit code 2 (not 0)."""
        # Arrange — valid PASS body so the path reaches send_alert.
        result_file = tmp_path / "smoke-result.json"
        body = _smoke_completed_body()
        body["structural_problems"] = []
        result_file.write_text(json.dumps(body))

        # Act
        with patch(
            "msai.services.alerting.AlertingService.send_alert",
            side_effect=RuntimeError("disk full"),
        ):
            invocation = runner.invoke(app, ["system", "smoke-alert", str(result_file)])

        # Assert — non-zero exit so the workflow sees the failure.
        assert invocation.exit_code == 2, invocation.output
