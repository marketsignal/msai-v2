"""Unit tests for the CLI completeness (REST parity) commands.

Covers T1-T10 of ``docs/plans/2026-05-15-cli-completeness.md``: 28 new
commands across 11 sub-apps (4 new + 7 modified). Each TestClass exercises
one command family; tests mock ``httpx.request`` and verify method + URL +
body / query params + output handling.

Why patch ``msai.cli.httpx.request``: the CLI's ``_api_call`` helper calls
``httpx.request(...)`` directly, so patching that attribute on the
``msai.cli`` module is the surgical seam. T9 symbol commands route through
the same helper via ``from msai.cli import _api_call`` — they are also
patched at ``msai.cli.httpx.request``.
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


def _ok_response(
    body: dict[str, Any] | list[Any] | None = None,
    *,
    status_code: int = 200,
    text: str | None = None,
) -> MagicMock:
    """Build a MagicMock httpx.Response equivalent for success cases."""
    response = MagicMock(spec=httpx.Response)
    response.is_success = True
    response.status_code = status_code
    if body is not None:
        response.json.return_value = body
        response.text = json.dumps(body)
    if text is not None:
        response.text = text
    return response


def _empty_204() -> MagicMock:
    """Response shape for a 204 No Content (symbols delete) — empty body.

    Crucially, ``.json()`` is NOT configured; calling it should fail the
    test if the CLI tries.
    """
    response = MagicMock(spec=httpx.Response)
    response.is_success = True
    response.status_code = 204
    response.text = ""
    return response


# ======================================================================
# T1 — alerts list
# ======================================================================


class TestAlertsList:
    def test_default_limit_50(self, runner: CliRunner) -> None:
        body = {"alerts": [{"id": "a1", "kind": "info"}]}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["alerts", "list"])
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/alerts/" in args[1]
        assert kwargs["params"] == {"limit": 50}

    def test_custom_limit_passes_verbatim(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request", return_value=_ok_response({"alerts": []})) as mock:
            result = runner.invoke(app, ["alerts", "list", "--limit", "200"])
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["params"] == {"limit": 200}


# ======================================================================
# T2 — strategy edit / delete
# ======================================================================


class TestStrategyEdit:
    def test_patches_description_only(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response({"id": "s1", "description": "new"})
        ) as mock:
            result = runner.invoke(app, ["strategy", "edit", "s1", "--description", "new"])
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "PATCH"
        assert "/api/v1/strategies/s1" in args[1]
        assert kwargs["json"] == {"description": "new"}

    def test_patches_default_config_from_literal(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request", return_value=_ok_response({"id": "s1"})) as mock:
            result = runner.invoke(
                app,
                ["strategy", "edit", "s1", "--default-config", '{"fast": 10}'],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.call_args
        assert kwargs["json"] == {"default_config": {"fast": 10}}

    def test_patches_default_config_from_file(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"fast": 7, "slow": 20}))
        with patch("msai.cli.httpx.request", return_value=_ok_response({"id": "s1"})) as mock:
            result = runner.invoke(
                app,
                [
                    "strategy",
                    "edit",
                    "s1",
                    "--description",
                    "v2",
                    "--default-config",
                    f"@{cfg}",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.call_args
        assert kwargs["json"] == {
            "description": "v2",
            "default_config": {"fast": 7, "slow": 20},
        }

    def test_no_fields_fails(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as mock:
            result = runner.invoke(app, ["strategy", "edit", "s1"])
        assert result.exit_code != 0
        assert mock.call_count == 0

    def test_empty_description_clears_field(self, runner: CliRunner) -> None:
        """Codex code-review iter-1 P2: `--description ""` is a deliberate
        clear, not an omission. The truthiness check would have dropped
        it; `is not None` preserves it."""
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response({"id": "s1", "description": ""})
        ) as mock:
            result = runner.invoke(app, ["strategy", "edit", "s1", "--description", ""])
        assert result.exit_code == 0, result.output
        _, kwargs = mock.call_args
        assert kwargs["json"] == {"description": ""}


class TestStrategyDelete:
    def test_delete_with_yes_skips_prompt(self, runner: CliRunner) -> None:
        body = {"message": "Strategy s1 deleted"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["strategy", "delete", "s1", "--yes"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "DELETE"
        assert "/api/v1/strategies/s1" in args[1]
        assert "deleted" in result.output

    def test_delete_aborts_without_confirmation(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as mock:
            result = runner.invoke(app, ["strategy", "delete", "s1"], input="n\n")
        assert result.exit_code != 0
        assert mock.call_count == 0



# ======================================================================
# T3 — graduation create / stage
# ======================================================================


class TestGraduationCreate:
    def test_posts_minimal_payload(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg.json"
        cfg.write_text(json.dumps({"fast": 5, "slow": 20}))
        body = {"id": "g1", "stage": "discovery"}
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)
        ) as mock:
            result = runner.invoke(
                app,
                [
                    "graduation",
                    "create",
                    "--strategy-id",
                    "sid-1",
                    "--config",
                    f"@{cfg}",
                ],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "POST"
        assert "/api/v1/graduation/candidates" in args[1]
        # metrics defaults to {} per the schema's required-but-empty default.
        assert kwargs["json"] == {
            "strategy_id": "sid-1",
            "config": {"fast": 5, "slow": 20},
            "metrics": {},
        }

    def test_posts_full_payload_with_metrics_research_job_and_notes(
        self, runner: CliRunner
    ) -> None:
        body = {"id": "g2"}
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)
        ) as mock:
            result = runner.invoke(
                app,
                [
                    "graduation",
                    "create",
                    "--strategy-id",
                    "sid-2",
                    "--config",
                    '{"k": 1}',
                    "--metrics",
                    '{"sharpe": 1.4}',
                    "--research-job-id",
                    "rj-1",
                    "--notes",
                    "from sweep",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.call_args
        assert kwargs["json"] == {
            "strategy_id": "sid-2",
            "config": {"k": 1},
            "metrics": {"sharpe": 1.4},
            "research_job_id": "rj-1",
            "notes": "from sweep",
        }


class TestGraduationStage:
    def test_posts_stage_only(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request", return_value=_ok_response({"id": "g1"})) as mock:
            result = runner.invoke(
                app,
                ["graduation", "stage", "g1", "--stage", "paper_candidate"],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "POST"
        assert "/api/v1/graduation/candidates/g1/stage" in args[1]
        assert kwargs["json"] == {"stage": "paper_candidate"}

    def test_posts_stage_with_reason(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request", return_value=_ok_response({"id": "g1"})) as mock:
            result = runner.invoke(
                app,
                [
                    "graduation",
                    "stage",
                    "g1",
                    "--stage",
                    "archived",
                    "--reason",
                    "bad sharpe",
                ],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["json"] == {"stage": "archived", "reason": "bad sharpe"}


# ======================================================================
# T4 — research sweep / walk-forward / promote
# ======================================================================


class TestResearchSweep:
    def test_posts_flat_body_from_file(self, runner: CliRunner, tmp_path: Path) -> None:
        cfg = tmp_path / "sweep.json"
        cfg.write_text(
            json.dumps(
                {
                    "strategy_id": "sid-1",
                    "instruments": ["AAPL"],
                    "start_date": "2024-01-01",
                    "end_date": "2024-06-01",
                    "parameter_grid": {"fast": [5, 10]},
                }
            )
        )
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response({"id": "rj-1"}, status_code=201)
        ) as mock:
            result = runner.invoke(app, ["research", "sweep", "--config", f"@{cfg}"])
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "POST"
        assert "/api/v1/research/sweeps" in args[1]
        # CRITICAL: the file IS the full body — NOT wrapped in {strategy_id, config}.
        assert kwargs["json"]["parameter_grid"] == {"fast": [5, 10]}
        assert "config" not in kwargs["json"]

    def test_rejects_invalid_json_literal(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as mock:
            result = runner.invoke(app, ["research", "sweep", "--config", "not-json"])
        assert result.exit_code != 0
        assert mock.call_count == 0


class TestResearchWalkForward:
    def test_posts_flat_body(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response({"id": "rj-2"}, status_code=201)
        ) as mock:
            result = runner.invoke(
                app,
                [
                    "research",
                    "walk-forward",
                    "--config",
                    '{"strategy_id": "s", "instruments": ["AAPL"], '
                    '"start_date": "2024-01-01", "end_date": "2024-06-01", '
                    '"parameter_grid": {}, "train_days": 60, "test_days": 20}',
                ],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "POST"
        assert "/api/v1/research/walk-forward" in args[1]
        assert kwargs["json"]["train_days"] == 60
        assert kwargs["json"]["test_days"] == 20


class TestResearchPromote:
    def test_posts_research_job_id_only(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response({"id": "c1"}, status_code=201)
        ) as mock:
            result = runner.invoke(app, ["research", "promote", "--job-id", "rj-1"])
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "POST"
        assert "/api/v1/research/promotions" in args[1]
        # CRITICAL: body key is `research_job_id`, NOT `job_id`.
        assert kwargs["json"] == {"research_job_id": "rj-1"}

    def test_posts_with_trial_index_and_notes(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response({"id": "c1"}, status_code=201)
        ) as mock:
            result = runner.invoke(
                app,
                [
                    "research",
                    "promote",
                    "--job-id",
                    "rj-1",
                    "--trial-index",
                    "3",
                    "--notes",
                    "best of batch",
                ],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["json"] == {
            "research_job_id": "rj-1",
            "trial_index": 3,
            "notes": "best of batch",
        }


# ======================================================================
# T5 — backtest report / trades
# ======================================================================


class TestBacktestReport:
    def test_two_step_flow_writes_html_to_out(self, runner: CliRunner, tmp_path: Path) -> None:
        token_body = {"signed_url": "/api/v1/backtests/bt1/report?token=abc", "expires_at": "x"}
        html_body = "<html>report body</html>"

        def _side_effect(method: str, url: str, **_kwargs: Any) -> MagicMock:
            if method == "POST" and "/report-token" in url:
                return _ok_response(token_body)
            if method == "GET" and "/report?token=" in url:
                return _ok_response(text=html_body)
            raise AssertionError(f"unexpected call: {method} {url}")

        with patch("msai.cli.httpx.request", side_effect=_side_effect) as mock:
            out_file = tmp_path / "r.html"
            result = runner.invoke(app, ["backtest", "report", "bt1", "--out", str(out_file)])
        assert result.exit_code == 0, result.output
        assert mock.call_count == 2
        # Verify both URLs hit.
        first_call = mock.call_args_list[0]
        assert first_call.args[0] == "POST"
        assert "/api/v1/backtests/bt1/report-token" in first_call.args[1]
        second_call = mock.call_args_list[1]
        assert second_call.args[0] == "GET"
        assert "report?token=abc" in second_call.args[1]
        assert out_file.read_text() == html_body

    def test_failure_when_signed_url_missing(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request",
            return_value=_ok_response({"expires_at": "x"}),  # no signed_url
        ):
            result = runner.invoke(app, ["backtest", "report", "bt1"])
        assert result.exit_code != 0
        assert "signed_url" in result.output.lower()


class TestBacktestTrades:
    def test_single_page_default_params(self, runner: CliRunner) -> None:
        body = {"items": [{"id": "t1"}], "total": 1, "page": 1, "page_size": 100}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["backtest", "trades", "bt1"])
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/backtests/bt1/trades" in args[1]
        assert kwargs["params"] == {"page": 1, "page_size": 100}

    def test_custom_page_and_page_size(self, runner: CliRunner) -> None:
        body = {"items": [], "total": 0, "page": 2, "page_size": 50}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(
                app,
                ["backtest", "trades", "bt1", "--page", "2", "--page-size", "50"],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["params"] == {"page": 2, "page_size": 50}

    def test_all_flag_loops_pages_using_server_page_size(self, runner: CliRunner) -> None:
        # Server clamps page_size to 500. We pass 1000 but the server echoes 500.
        # Use server's page_size to decide when to stop.
        page1 = {
            "items": [{"id": f"t{i}"} for i in range(500)],
            "total": 750,
            "page": 1,
            "page_size": 500,
        }
        page2 = {
            "items": [{"id": f"t{i}"} for i in range(500, 750)],
            "total": 750,
            "page": 2,
            "page_size": 500,
        }
        responses = [_ok_response(page1), _ok_response(page2)]
        with patch("msai.cli.httpx.request", side_effect=responses) as mock:
            result = runner.invoke(
                app, ["backtest", "trades", "bt1", "--page-size", "1000", "--all"]
            )
        assert result.exit_code == 0, result.output
        assert mock.call_count == 2
        merged = json.loads(result.output)
        assert len(merged["items"]) == 750
        assert merged["pages_fetched"] == 2

    def test_writes_to_out_file(self, runner: CliRunner, tmp_path: Path) -> None:
        body = {"items": [], "total": 0, "page": 1, "page_size": 100}
        out = tmp_path / "trades.json"
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)):
            result = runner.invoke(app, ["backtest", "trades", "bt1", "--out", str(out)])
        assert result.exit_code == 0, result.output
        assert json.loads(out.read_text()) == body


# ======================================================================
# T6 — live status-show + portfolio list/show/draft-members
# ======================================================================


class TestLiveStatusShow:
    def test_gets_single_deployment(self, runner: CliRunner) -> None:
        body = {"id": "dep-1", "status": "running"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["live", "status-show", "dep-1"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/live/status/dep-1" in args[1]


class TestLivePortfolioList:
    def test_lists_live_portfolios(self, runner: CliRunner) -> None:
        body = [{"id": "p1", "name": "drill"}]
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["live", "portfolio-list"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert args[1].endswith("/api/v1/live-portfolios")


class TestLivePortfolioShow:
    def test_gets_one_portfolio(self, runner: CliRunner) -> None:
        body = {"id": "p1", "name": "drill"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["live", "portfolio-show", "p1"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/live-portfolios/p1" in args[1]


class TestLivePortfolioDraftMembers:
    def test_gets_draft_members(self, runner: CliRunner) -> None:
        body = [{"id": "m1", "strategy_id": "s1"}]
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["live", "portfolio-draft-members", "p1"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/live-portfolios/p1/members" in args[1]


# ======================================================================
# T7 — portfolio create / run-show / run-report
# ======================================================================


class TestPortfolioCreate:
    def test_posts_config_body(self, runner: CliRunner) -> None:
        body = {"id": "pf-1"}
        payload_literal = json.dumps(
            {
                "name": "Sharpe-1",
                "objective": "maximize_sharpe",
                "base_capital": 100000.0,
                "allocations": [{"candidate_id": "c1", "weight": 0.5}],
            }
        )
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response(body, status_code=201)
        ) as mock:
            result = runner.invoke(app, ["portfolio", "create", "--config", payload_literal])
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "POST"
        # NB: existing portfolio_app uses "/api/v1/portfolios" — verify the
        # new `create` hits the same prefix.
        assert "/api/v1/portfolios" in args[1]
        assert kwargs["json"]["objective"] == "maximize_sharpe"


class TestPortfolioRunShow:
    def test_gets_single_run(self, runner: CliRunner) -> None:
        body = {"id": "run-1", "status": "completed"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["portfolio", "run-show", "run-1"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/portfolios/runs/run-1" in args[1]


class TestPortfolioRunReport:
    def test_writes_html_to_out(self, runner: CliRunner, tmp_path: Path) -> None:
        html_body = "<html>portfolio report</html>"
        out = tmp_path / "p.html"
        with patch("msai.cli.httpx.request", return_value=_ok_response(text=html_body)) as mock:
            result = runner.invoke(app, ["portfolio", "run-report", "run-1", "--out", str(out)])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/portfolios/runs/run-1/report" in args[1]
        assert out.read_text() == html_body


# ======================================================================
# T8 — market-data sub-app
# ======================================================================


class TestMarketDataBars:
    def test_gets_bars_with_query_params(self, runner: CliRunner) -> None:
        body = {"symbol": "AAPL", "interval": "1m", "bars": [], "count": 0}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(
                app,
                [
                    "market-data",
                    "bars",
                    "AAPL",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-06-01",
                ],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/market-data/bars/AAPL" in args[1]
        assert kwargs["params"] == {
            "start": "2024-01-01",
            "end": "2024-06-01",
            "interval": "1m",
        }

    def test_custom_interval(self, runner: CliRunner) -> None:
        body = {"symbol": "AAPL", "interval": "5m", "bars": [], "count": 0}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(
                app,
                [
                    "market-data",
                    "bars",
                    "AAPL",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-06-01",
                    "--interval",
                    "5m",
                ],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["params"]["interval"] == "5m"


class TestMarketDataSymbols:
    def test_gets_symbols(self, runner: CliRunner) -> None:
        body = {"symbols": {"equity": ["AAPL", "MSFT"]}}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["market-data", "symbols"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/market-data/symbols" in args[1]


class TestMarketDataStatus:
    def test_gets_status(self, runner: CliRunner) -> None:
        body = {
            "status": "ok",
            "storage": {"asset_classes": {"stocks": 2}, "total_files": 5, "total_bytes": 1024},
        }
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["market-data", "status"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/market-data/status" in args[1]


class TestMarketDataIngest:
    def test_posts_ingest_request(self, runner: CliRunner) -> None:
        body = {
            "message": "queued",
            "asset_class": "stocks",
            "symbols": ["AAPL", "MSFT"],
            "start": "2024-01-01",
            "end": "2024-06-01",
        }
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response(body, status_code=202)
        ) as mock:
            result = runner.invoke(
                app,
                [
                    "market-data",
                    "ingest",
                    "--asset-class",
                    "stocks",
                    "--symbols",
                    "AAPL,MSFT",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-06-01",
                ],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "POST"
        assert "/api/v1/market-data/ingest" in args[1]
        assert kwargs["json"] == {
            "asset_class": "stocks",
            "symbols": ["AAPL", "MSFT"],
            "start": "2024-01-01",
            "end": "2024-06-01",
            "provider": "auto",
        }

    def test_dataset_and_data_schema_overrides(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request",
            return_value=_ok_response(
                {
                    "message": "queued",
                    "asset_class": "futures",
                    "symbols": ["ES.n.0"],
                    "start": "2024-01-01",
                    "end": "2024-06-01",
                },
                status_code=202,
            ),
        ) as mock:
            result = runner.invoke(
                app,
                [
                    "market-data",
                    "ingest",
                    "--asset-class",
                    "futures",
                    "--symbols",
                    "ES.n.0",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-06-01",
                    "--provider",
                    "databento",
                    "--dataset",
                    "GLBX.MDP3",
                    "--data-schema",
                    "ohlcv-1m",
                ],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["json"]["dataset"] == "GLBX.MDP3"
        assert kwargs["json"]["data_schema"] == "ohlcv-1m"
        assert kwargs["json"]["provider"] == "databento"

    def test_rejects_empty_symbols(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as mock:
            result = runner.invoke(
                app,
                [
                    "market-data",
                    "ingest",
                    "--asset-class",
                    "stocks",
                    "--symbols",
                    " ,, ",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-06-01",
                ],
            )
        assert result.exit_code != 0
        assert mock.call_count == 0


# ======================================================================
# T9 — symbols inventory / readiness / delete
# ======================================================================


class TestSymbolsInventory:
    def test_no_filters(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request", return_value=_ok_response([])) as mock:
            result = runner.invoke(app, ["symbols", "inventory"])
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/symbols/inventory" in args[1]
        # With no filters, params should be None (or empty) — _api_call treats
        # None and {} alike, so just verify no forbidden keys.
        params = kwargs.get("params") or {}
        assert "limit" not in params

    def test_window_and_asset_class(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request", return_value=_ok_response([])) as mock:
            result = runner.invoke(
                app,
                [
                    "symbols",
                    "inventory",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-06-01",
                    "--asset-class",
                    "equity",
                ],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.call_args
        assert kwargs["params"] == {
            "start": "2024-01-01",
            "end": "2024-06-01",
            "asset_class": "equity",
        }

    def test_rejects_invalid_asset_class(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as mock:
            result = runner.invoke(app, ["symbols", "inventory", "--asset-class", "stocks"])
        # `stocks` is the ingest-side alias, NOT the registry taxonomy.
        assert result.exit_code != 0
        assert mock.call_count == 0


class TestSymbolsReadiness:
    def test_requires_both_symbol_and_asset_class(self, runner: CliRunner) -> None:
        body = {"registered": True, "live_qualified": False}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(
                app,
                [
                    "symbols",
                    "readiness",
                    "--symbol",
                    "AAPL.NASDAQ",
                    "--asset-class",
                    "equity",
                ],
            )
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/symbols/readiness" in args[1]
        assert kwargs["params"] == {
            "symbol": "AAPL.NASDAQ",
            "asset_class": "equity",
        }

    def test_with_window(self, runner: CliRunner) -> None:
        with patch(
            "msai.cli.httpx.request", return_value=_ok_response({"registered": True})
        ) as mock:
            result = runner.invoke(
                app,
                [
                    "symbols",
                    "readiness",
                    "--symbol",
                    "AAPL.NASDAQ",
                    "--asset-class",
                    "equity",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-06-01",
                ],
            )
        assert result.exit_code == 0
        _, kwargs = mock.call_args
        assert kwargs["params"] == {
            "symbol": "AAPL.NASDAQ",
            "asset_class": "equity",
            "start": "2024-01-01",
            "end": "2024-06-01",
        }


class TestSymbolsDelete:
    def test_204_no_body_synthesizes_success(self, runner: CliRunner) -> None:
        # CRITICAL: the response is 204 EMPTY BODY. The CLI MUST NOT call
        # response.json() — _empty_204() does not configure .json(), so a
        # bad implementation would raise MagicMock's auto-spec'd return.
        with patch("msai.cli.httpx.request", return_value=_empty_204()) as mock:
            result = runner.invoke(
                app, ["symbols", "delete", "AAPL", "--asset-class", "equity", "--yes"]
            )
        assert result.exit_code == 0, result.output
        args, kwargs = mock.call_args
        assert args[0] == "DELETE"
        assert "/api/v1/symbols/AAPL" in args[1]
        assert kwargs["params"] == {"asset_class": "equity"}
        assert "Deleted AAPL" in result.output

    def test_confirm_prompt_default(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as mock:
            result = runner.invoke(
                app,
                ["symbols", "delete", "AAPL", "--asset-class", "equity"],
                input="n\n",
            )
        assert result.exit_code != 0
        assert mock.call_count == 0

    def test_rejects_slash_bearing_symbol(self, runner: CliRunner) -> None:
        with patch("msai.cli.httpx.request") as mock:
            result = runner.invoke(
                app,
                [
                    "symbols",
                    "delete",
                    "EUR/USD.IDEALPRO",
                    "--asset-class",
                    "fx",
                    "--yes",
                ],
            )
        assert result.exit_code != 0
        assert mock.call_count == 0
        assert "slash" in result.output.lower()


# ======================================================================
# T10 — auth me / logout (+ whoami alias)
# ======================================================================


class TestAuthMe:
    def test_gets_auth_me(self, runner: CliRunner) -> None:
        body = {"sub": "user-1", "preferred_username": "pablo"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["auth", "me"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/auth/me" in args[1]
        assert "pablo" in result.output


class TestAuthLogout:
    def test_posts_logout_returns_message(self, runner: CliRunner) -> None:
        body = {"message": "Logged out"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["auth", "logout"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "POST"
        assert "/api/v1/auth/logout" in args[1]
        assert "Logged out" in result.output


class TestWhoamiAlias:
    def test_whoami_hits_auth_me_endpoint(self, runner: CliRunner) -> None:
        body = {"sub": "user-1"}
        with patch("msai.cli.httpx.request", return_value=_ok_response(body)) as mock:
            result = runner.invoke(app, ["whoami"])
        assert result.exit_code == 0, result.output
        args, _ = mock.call_args
        assert args[0] == "GET"
        assert "/api/v1/auth/me" in args[1]


# ======================================================================
# Command-tree structural check — verify every new sub-app is registered.
# ======================================================================


class TestCommandTreeRegistration:
    def test_root_help_lists_new_sub_apps(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for sub in ("alerts", "auth", "market-data"):
            assert sub in result.output

    def test_root_help_lists_whoami_top_level(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "whoami" in result.output

    @pytest.mark.parametrize(
        ("sub_app", "expected_commands"),
        [
            ("alerts", {"list"}),
            ("auth", {"me", "logout"}),
            ("market-data", {"bars", "ingest", "status", "symbols"}),
        ],
    )
    def test_new_sub_apps_register_expected_commands(
        self, runner: CliRunner, sub_app: str, expected_commands: set[str]
    ) -> None:
        result = runner.invoke(app, [sub_app, "--help"])
        assert result.exit_code == 0
        for command in expected_commands:
            assert command in result.output

    @pytest.mark.parametrize(
        ("sub_app", "new_commands"),
        [
            ("strategy", {"edit", "delete"}),
            ("backtest", {"report", "trades"}),
            ("research", {"sweep", "walk-forward", "promote"}),
            ("graduation", {"create", "stage"}),
            (
                "live",
                {
                    "status-show",
                    "portfolio-list",
                    "portfolio-show",
                    "portfolio-draft-members",
                },
            ),
            ("portfolio", {"create", "run-show", "run-report"}),
            ("symbols", {"inventory", "readiness", "delete"}),
        ],
    )
    def test_modified_sub_apps_register_new_commands(
        self, runner: CliRunner, sub_app: str, new_commands: set[str]
    ) -> None:
        result = runner.invoke(app, [sub_app, "--help"])
        assert result.exit_code == 0
        for command in new_commands:
            assert command in result.output
