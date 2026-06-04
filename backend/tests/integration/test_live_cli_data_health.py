"""Integration tests for the ``msai live data-health`` CLI command (PR 1b T8).

The CLI is a THIN HTTP CLIENT of ``GET /api/v1/live/data-health`` — it does not
touch the DB/KV/service layer.  It mirrors ``msai live status`` exactly: same
env-var auth (``MSAI_API_URL`` / ``MSAI_API_KEY``), same ``_api_call``
error-surface discipline (non-2xx → API error body to stderr + exit 1).

These tests assert the OPERATOR-VISIBLE rendering:

- healthy feeds → a table with account / dataset / symbol / verdict per feed;
- an empty fleet → an explicit "no active feeds" line (never a blank table);
- ``fleet_halted`` with a ``data_stale`` cause → a banner naming ``data_stale``
  AND the source feed (so the operator sees WHICH feed went stale);
- ``fleet_halted`` with a ``fleet_emergency`` (manual kill-all) cause → a banner
  that distinguishes it from a data-stale halt;
- a 503 from the API → the error body is surfaced to stderr + a non-zero exit.

We stub the HTTP layer (``httpx.request``) so the *real* ``_api_call`` runs,
including its non-2xx → stderr/``Exit`` error path — that's the behavior under
test.  We capture the outgoing request to assert which route the CLI hit.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from msai.cli import app

# rich colorizes option/command names in --help, which in a TTY/CI terminal
# splits literals across SGR escape spans (e.g. "--data" + "-health"); locally
# (no color) it is contiguous. Strip ANSI escapes AND collapse whitespace before
# matching so the assertion holds in both renders. (Mirrors
# test_live_cli_broker_account.py:245-262.)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _clean(out: str) -> str:
    return " ".join(_ANSI.sub("", out).split())


def _healthy_feeds_response(method: str, url: str) -> httpx.Response:
    """Two healthy feeds across two accounts, fleet NOT halted."""
    return httpx.Response(
        200,
        json={
            "feeds": [
                {
                    "account_id": "DU1234567",
                    "node_id": "node-aapl",
                    "deployment_id": "dep-aapl",
                    "dataset": "EQUS.MINI",
                    "feed": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
                    "symbol": "AAPL",
                    "last_event_ts": 1_700_000_000_000_000_000,
                    "phase": "RTH",
                    "grace_s": 90,
                    "verdict": "fresh",
                    "published_at": "2026-06-03T14:00:00+00:00",
                },
                {
                    "account_id": "U4705114",
                    "node_id": "node-es",
                    "deployment_id": "dep-es",
                    "dataset": "GLBX.MDP3",
                    "feed": "ES.GLBX-1-MINUTE-LAST-EXTERNAL",
                    "symbol": "ES",
                    "last_event_ts": 1_700_000_000_000_000_000,
                    "phase": "RTH",
                    "grace_s": 60,
                    "verdict": "fresh",
                    "published_at": "2026-06-03T14:00:00+00:00",
                },
            ],
            "fleet_halted": False,
            "halt_cause": None,
        },
        request=httpx.Request(method, url),
    )


def _empty_feeds_response(method: str, url: str) -> httpx.Response:
    """No active deployments — empty fleet, not halted."""
    return httpx.Response(
        200,
        json={"feeds": [], "fleet_halted": False, "halt_cause": None},
        request=httpx.Request(method, url),
    )


def _data_stale_halt_response(method: str, url: str) -> httpx.Response:
    """Fleet halted by the in-node data-stale monitor (PR 1b cause JSON)."""
    return httpx.Response(
        200,
        json={
            "feeds": [
                {
                    "account_id": "DU1234567",
                    "node_id": "node-aapl",
                    "deployment_id": "dep-aapl",
                    "dataset": "EQUS.MINI",
                    "feed": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
                    "symbol": "AAPL",
                    "last_event_ts": 1_700_000_000_000_000_000,
                    "phase": "RTH",
                    "grace_s": 90,
                    "verdict": "stale",
                    "published_at": "2026-06-03T14:05:00+00:00",
                },
            ],
            "fleet_halted": True,
            "halt_cause": {
                "reason": "data_stale",
                "account_id": "DU1234567",
                "node_id": "node-aapl",
                "deployment_id": "dep-aapl",
                "dataset": "EQUS.MINI",
                "feed": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
                "symbol": "AAPL",
                "detected_at": "2026-06-03T14:05:00+00:00",
                "last_event_ts": 1_700_000_000_000_000_000,
            },
        },
        request=httpx.Request(method, url),
    )


def _manual_halt_response(method: str, url: str) -> httpx.Response:
    """Fleet halted by a manual kill-all (fleet_emergency cause)."""
    return httpx.Response(
        200,
        json={
            "feeds": [],
            "fleet_halted": True,
            "halt_cause": {
                "reason": "fleet_emergency",
                "set_by": "operator:kill-all",
            },
        },
        request=httpx.Request(method, url),
    )


def _service_unavailable_response(method: str, url: str) -> httpx.Response:
    """A 503 the API returns when the command bus / Redis is unreachable."""
    return httpx.Response(
        503,
        json={
            "error": {
                "code": "SERVICE_UNAVAILABLE",
                "message": "command bus is not connected",
            }
        },
        request=httpx.Request(method, url),
    )


@pytest.fixture
def capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``httpx.request`` so the real ``_api_call`` runs; capture the call."""
    captured: dict[str, Any] = {}

    def _make_stub(responder):  # type: ignore[no-untyped-def]
        def _stub(method, url, *, json=None, params=None, headers=None, timeout=None):  # type: ignore[no-untyped-def]
            captured["method"] = method
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers
            return responder(method, url)

        return _stub

    captured["_install"] = lambda responder: monkeypatch.setattr(
        "msai.cli.httpx.request", _make_stub(responder)
    )
    return captured


def test_data_health_healthy_feeds_renders_table(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_healthy_feeds_response)

    result = CliRunner().invoke(app, ["live", "data-health"])

    assert result.exit_code == 0, result.output
    # It hit the read-only route (GET — never mutates).
    assert capture_request["method"] == "GET"
    assert capture_request["url"].endswith("/api/v1/live/data-health")
    out = _clean(result.stdout)
    # Each feed's operator-facing columns are present.
    assert "DU1234567" in out
    assert "U4705114" in out
    assert "EQUS.MINI" in out
    assert "GLBX.MDP3" in out
    assert "AAPL" in out
    assert "ES" in out
    assert "fresh" in out
    # A healthy fleet shows the not-halted line, never the halt banner.
    assert "Fleet halted: False" in out
    assert "FLEET HALTED —" not in out


def test_data_health_empty_fleet_shows_no_active_feeds_line(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_empty_feeds_response)

    result = CliRunner().invoke(app, ["live", "data-health"])

    assert result.exit_code == 0, result.output
    out = _clean(result.stdout).lower()
    assert "no active feeds" in out


def test_data_health_data_stale_halt_banner_names_cause_and_feed(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_data_stale_halt_response)

    result = CliRunner().invoke(app, ["live", "data-health"])

    assert result.exit_code == 0, result.output
    out = _clean(result.stdout)
    # Banner must announce the fleet is halted.
    assert "HALTED" in out.upper()
    # The operator must see WHICH cause — data_stale, distinguishable.
    assert "data_stale" in out
    # ...and the source feed that went stale (so they know where to look).
    assert "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL" in out


def test_data_health_manual_halt_banner_distinguishes_cause(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_manual_halt_response)

    result = CliRunner().invoke(app, ["live", "data-health"])

    assert result.exit_code == 0, result.output
    out = _clean(result.stdout)
    assert "HALTED" in out.upper()
    # A manual kill-all is distinguished from a data-stale halt.
    assert "fleet_emergency" in out
    assert "data_stale" not in out


def test_data_health_api_503_surfaces_error_to_stderr_exit_1(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_service_unavailable_response)

    result = CliRunner().invoke(app, ["live", "data-health"])

    assert result.exit_code == 1
    # The API error is surfaced to stderr (not swallowed).
    assert "503" in result.stderr
    assert "SERVICE_UNAVAILABLE" in result.stderr


def test_data_health_listed_in_help() -> None:
    runner = CliRunner()
    live_help = runner.invoke(app, ["live", "--help"])
    cmd_help = runner.invoke(app, ["live", "data-health", "--help"])
    assert live_help.exit_code == 0 and cmd_help.exit_code == 0
    # The command shows up in the `live` sub-app help...
    assert "data-health" in _clean(live_help.stdout)
    # ...and its own --help renders (exit 0 already asserts it resolves).
    assert "data-health" in _clean(cmd_help.stdout) or "Usage" in _clean(cmd_help.stdout)
