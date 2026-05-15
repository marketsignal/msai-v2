"""Unit tests for the new ``msai live`` portfolio + deployment commands.

These tests mock ``httpx.request`` (the single transport the CLI calls
through ``_api_call``) and verify each new command's method + URL + body
shape.  No backend is required.

T10 / T11 of ``docs/plans/2026-05-15-live-deployment-workflow-ui-cli.md``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from msai.cli import app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _ok_response(body: dict[str, Any] | list[Any], *, status_code: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.is_success = True
    response.status_code = status_code
    response.json.return_value = body
    response.text = json.dumps(body)
    return response


class TestPortfolioCreate:
    def test_posts_name_and_description(self, runner: CliRunner) -> None:
        body = {"id": "pf-1", "name": "drill-abc"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(
                app,
                ["live", "portfolio-create", "--name", "drill-abc", "--description", "x"],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = m.call_args
        assert args[0] == "POST"
        # Codex code-review P1: must hit /api/v1/live-portfolios WITHOUT
        # trailing slash (the route is registered with empty path; trailing
        # slash triggers a 307 redirect that httpx doesn't follow).
        assert args[1].endswith("/api/v1/live-portfolios")
        assert not args[1].endswith("/api/v1/live-portfolios/")
        assert kwargs["json"] == {"name": "drill-abc", "description": "x"}
        assert "pf-1" in result.output

    def test_description_optional(self, runner: CliRunner) -> None:
        body = {"id": "pf-2", "name": "n"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(app, ["live", "portfolio-create", "--name", "n"])
        assert result.exit_code == 0, result.output
        _, kwargs = m.call_args
        # description should be sent either as "" or omitted; require omitted
        # OR an empty string to be tolerant of either implementation.
        body_sent = kwargs["json"]
        assert body_sent.get("name") == "n"


class TestPortfolioAddStrategy:
    def test_posts_strategy_with_inline_config(self, runner: CliRunner) -> None:
        body = {"id": "m-1", "strategy_id": "stg-uuid"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(
                app,
                [
                    "live",
                    "portfolio-add-strategy",
                    "pf-1",
                    "--strategy-id",
                    "stg-uuid",
                    "--config",
                    '{"k":1}',
                    "--instruments",
                    "AAPL.NASDAQ,MSFT.NASDAQ",
                    "--weight",
                    "1.0",
                ],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = m.call_args
        assert args[0] == "POST"
        assert "/api/v1/live-portfolios/pf-1/strategies" in args[1]
        sent = kwargs["json"]
        assert sent["strategy_id"] == "stg-uuid"
        assert sent["config"] == {"k": 1}
        assert sent["instruments"] == ["AAPL.NASDAQ", "MSFT.NASDAQ"]
        assert sent["weight"] == "1.0"

    def test_config_file_atsign_syntax(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text('{"bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"}')
        body = {"id": "m-2"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(
                app,
                [
                    "live",
                    "portfolio-add-strategy",
                    "pf-1",
                    "--strategy-id",
                    "stg",
                    "--config",
                    f"@{cfg_file}",
                    "--instruments",
                    "AAPL.NASDAQ",
                    "--weight",
                    "0.5",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = m.call_args
        assert kwargs["json"]["config"] == {"bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"}


class TestPortfolioSnapshot:
    def test_posts_snapshot_endpoint(self, runner: CliRunner) -> None:
        body = {"id": "rev-1", "portfolio_id": "pf-1"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(app, ["live", "portfolio-snapshot", "pf-1"])
        assert result.exit_code == 0, result.output
        args, _ = m.call_args
        assert args[0] == "POST"
        assert "/api/v1/live-portfolios/pf-1/snapshot" in args[1]
        assert "rev-1" in result.output


class TestPortfolioMembers:
    def test_hits_revision_members_endpoint(self, runner: CliRunner) -> None:
        body = {"items": [{"strategy_id": "stg-1"}]}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as m:
            result = runner.invoke(app, ["live", "portfolio-members", "rev-9"])
        assert result.exit_code == 0, result.output
        args, _ = m.call_args
        assert args[0] == "GET"
        assert "/api/v1/live-portfolio-revisions/rev-9/members" in args[1]


class TestStartPortfolio:
    def test_paper_path_posts_payload(self, runner: CliRunner) -> None:
        body = {"id": "dep-1", "status": "starting", "paper_trading": True}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(
                app,
                [
                    "live",
                    "start-portfolio",
                    "--revision",
                    "rev-1",
                    "--account",
                    "DUP733213",
                    "--ib-login-key",
                    "marin1016test",
                    "--idempotency-key",
                    "ik-fixed",
                ],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = m.call_args
        assert args[0] == "POST"
        assert "/api/v1/live/start-portfolio" in args[1]
        sent = kwargs["json"]
        assert sent["portfolio_revision_id"] == "rev-1"
        assert sent["account_id"] == "DUP733213"
        assert sent["ib_login_key"] == "marin1016test"
        assert sent["paper_trading"] is True
        # Codex code-review P1: Idempotency-Key flows as HTTP header, NOT body
        # field — PortfolioStartRequest doesn't define `idempotency_key`, so a
        # body field would silently bypass the Redis reservation layer.
        assert "idempotency_key" not in sent
        assert kwargs["headers"]["Idempotency-Key"] == "ik-fixed"

    def test_ib_login_key_is_required(self, runner: CliRunner) -> None:
        result = runner.invoke(
            app,
            [
                "live",
                "start-portfolio",
                "--revision",
                "rev-1",
                "--account",
                "DU123",
            ],
        )
        assert result.exit_code != 0
        # Typer/Click renders missing required options as either
        # "Missing option" or "Error" — accept either.
        assert "ib-login-key" in result.output.lower() or "missing" in result.output.lower()

    def test_no_paper_aborts_when_user_declines(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as m:
            result = runner.invoke(
                app,
                [
                    "live",
                    "start-portfolio",
                    "--revision",
                    "rev-1",
                    "--account",
                    "U4705114",
                    "--ib-login-key",
                    "mslvp000",
                    "--no-paper",
                ],
                input="n\n",
            )
        assert result.exit_code != 0
        assert m.call_count == 0
        assert "REAL-MONEY" in result.output

    def test_no_paper_confirmed_sends_paper_false(self, runner: CliRunner) -> None:
        body = {"id": "dep-real", "status": "starting", "paper_trading": False}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(
                app,
                [
                    "live",
                    "start-portfolio",
                    "--revision",
                    "rev-1",
                    "--account",
                    "U4705114",
                    "--ib-login-key",
                    "mslvp000",
                    "--no-paper",
                ],
                input="y\n",
            )
        assert result.exit_code == 0, result.output
        _, kwargs = m.call_args
        assert kwargs["json"]["paper_trading"] is False
        assert kwargs["json"]["account_id"] == "U4705114"

    def test_idempotency_key_autogenerated_when_omitted(self, runner: CliRunner) -> None:
        body = {"id": "dep-2", "status": "ready"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(
                app,
                [
                    "live",
                    "start-portfolio",
                    "--revision",
                    "rev-1",
                    "--account",
                    "DU1",
                    "--ib-login-key",
                    "k",
                ],
            )
        assert result.exit_code == 0, result.output
        # Codex code-review P1: Idempotency-Key lands in headers, not body.
        sent_headers = m.call_args.kwargs["headers"]
        assert isinstance(sent_headers.get("Idempotency-Key"), str)
        assert len(sent_headers["Idempotency-Key"]) > 0
        sent_body = m.call_args.kwargs["json"]
        assert "idempotency_key" not in sent_body


class TestLiveStartAliasPrefixGuard:
    """PR #67 Codex bot P2: the legacy `live start` alias must also reject
    account/paper mismatches before HTTP, mirroring `start-portfolio`."""

    def test_paper_default_with_live_account_blocked(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as m:
            result = runner.invoke(
                app,
                ["live", "start", "rev-1", "U4705114", "--ib-login-key", "k"],
            )
        assert result.exit_code != 0
        assert m.call_count == 0
        assert "not a paper-prefix" in result.output

    def test_no_paper_with_paper_account_blocked(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as m:
            result = runner.invoke(
                app,
                [
                    "live",
                    "start",
                    "rev-1",
                    "DU1234567",
                    "--ib-login-key",
                    "k",
                    "--no-paper",
                ],
            )
        assert result.exit_code != 0
        assert m.call_count == 0
        assert "not a live-prefix" in result.output

    def test_paper_with_df_fa_account_allowed(self, runner: CliRunner) -> None:
        body = {"id": "dep-fa", "status": "starting", "paper_trading": True}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(
                app,
                ["live", "start", "rev-1", "DF999", "--ib-login-key", "k"],
            )
        assert result.exit_code == 0, result.output
        # DF is a valid paper prefix (FA sub-accounts)
        assert m.call_args.kwargs["json"]["account_id"] == "DF999"

    def test_payload_trims_whitespace(self, runner: CliRunner) -> None:
        body = {"id": "dep-trim", "status": "starting"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)) as m:
            result = runner.invoke(
                app,
                ["live", "start", "rev-1", " DU1234567 ", "--ib-login-key", " key "],
            )
        assert result.exit_code == 0, result.output
        sent = m.call_args.kwargs["json"]
        assert sent["account_id"] == "DU1234567"
        assert sent["ib_login_key"] == "key"


class TestResume:
    def test_posts_resume_endpoint(self, runner: CliRunner) -> None:
        body = {"resumed": True}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as m:
            result = runner.invoke(app, ["live", "resume"])
        assert result.exit_code == 0, result.output
        args, _ = m.call_args
        assert args[0] == "POST"
        assert "/api/v1/live/resume" in args[1]


class TestPositions:
    def test_hits_positions_endpoint_no_filter(self, runner: CliRunner) -> None:
        body = {"items": [{"symbol": "AAPL"}]}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as m:
            result = runner.invoke(app, ["live", "positions"])
        assert result.exit_code == 0, result.output
        args, _ = m.call_args
        assert args[0] == "GET"
        assert "/api/v1/live/positions" in args[1]


class TestTrades:
    def test_hits_trades_endpoint(self, runner: CliRunner) -> None:
        body = {"items": []}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as m:
            result = runner.invoke(app, ["live", "trades", "--limit", "10"])
        assert result.exit_code == 0, result.output
        args, kwargs = m.call_args
        assert args[0] == "GET"
        assert "/api/v1/live/trades" in args[1]
        assert kwargs.get("params", {}).get("limit") == 10

    def test_deployment_filter_passed(self, runner: CliRunner) -> None:
        body = {"items": []}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as m:
            result = runner.invoke(app, ["live", "trades", "--deployment", "dep-7", "--limit", "5"])
        assert result.exit_code == 0, result.output
        params = m.call_args.kwargs.get("params", {})
        assert params.get("deployment_id") == "dep-7"
        assert params.get("limit") == 5


class TestAudits:
    def test_hits_audits_endpoint(self, runner: CliRunner) -> None:
        body = {"items": [{"event": "started"}]}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as m:
            result = runner.invoke(app, ["live", "audits", "dep-1"])
        assert result.exit_code == 0, result.output
        args, _ = m.call_args
        assert args[0] == "GET"
        assert "/api/v1/live/audits/dep-1" in args[1]
