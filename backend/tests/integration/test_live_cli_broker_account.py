"""Integration tests for the ``--broker-account-id`` selector on the
``msai live start-portfolio`` and ``msai live start`` CLI commands (Task 6).

The CLI is a THIN HTTP CLIENT of ``POST /api/v1/live/start-portfolio`` — it
does not touch the DB/KV/service layer.  These tests assert:

- the selector-only form (``--broker-account-id`` with NO ``--account`` /
  ``--ib-login-key``) builds a POST body carrying ``broker_account_id`` and
  succeeds (the client-side prefix guard must NOT reject/require the raw
  account string when the selector is given — the either/or contract holds
  client-side too);
- when the (stubbed) API rejects the selector with the archived-account 422,
  the command surfaces the API error body to stderr and exits non-zero;
- the legacy ``--account DU... --ib-login-key ...`` form (no selector) still
  works (back-compat).

We stub the HTTP layer (``httpx.request``) so the *real* ``_api_call`` runs,
including its non-2xx → stderr/``Exit`` error path — that's the behavior under
test.  We capture the outgoing request body to assert what the CLI sent.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from msai.cli import app

BROKER_ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
REVISION_ID = "22222222-2222-2222-2222-222222222222"


def _success_response(method: str, url: str) -> httpx.Response:
    """A 201 deployment-created response shaped like /start-portfolio."""
    return httpx.Response(
        201,
        json={"id": "dep-uuid-1", "status": "starting"},
        request=httpx.Request(method, url),
    )


def _archived_422_response(method: str, url: str) -> httpx.Response:
    """The archived-broker-account 422 the API returns (Task 5 contract)."""
    return httpx.Response(
        422,
        json={
            "error": {
                "code": "BROKER_ACCOUNT_ARCHIVED",
                "message": "broker account is archived and cannot be deployed",
            }
        },
        request=httpx.Request(method, url),
    )


@pytest.fixture
def capture_request(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub ``httpx.request`` so the real ``_api_call`` runs; capture body."""
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


# ----------------------------------------------------------------------
# live start-portfolio
# ----------------------------------------------------------------------


def test_start_portfolio_broker_account_id_only_succeeds(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_success_response)
    result = CliRunner().invoke(
        app,
        [
            "live",
            "start-portfolio",
            "--revision",
            REVISION_ID,
            "--broker-account-id",
            BROKER_ACCOUNT_ID,
        ],
    )
    assert result.exit_code == 0, result.output
    body = capture_request["body"]
    assert body["broker_account_id"] == BROKER_ACCOUNT_ID
    assert body["portfolio_revision_id"] == REVISION_ID
    # selector-only: no raw account/ib_login_key should be required/sent
    assert "account_id" not in body or not body["account_id"]
    assert "dep-uuid-1" in result.stdout


def test_no_paper_confirm_names_deployed_broker_account_not_legacy(capture_request) -> None:  # type: ignore[no-untyped-def]
    """Real-money safety (Codex PR #88 review): when BOTH --broker-account-id and a
    legacy --account are supplied with --no-paper, the payload sends only
    broker_account_id (legacy dropped), so the REAL-MONEY confirmation prompt must
    name the broker account actually deployed — NOT the legacy account label."""
    capture_request["_install"](_success_response)
    result = CliRunner().invoke(
        app,
        [
            "live",
            "start-portfolio",
            "--revision",
            REVISION_ID,
            "--broker-account-id",
            BROKER_ACCOUNT_ID,
            "--account",
            "U9999999",
            "--ib-login-key",
            "legacy-login",
            "--no-paper",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    # The confirm names the deployed broker account, never the legacy U9999999.
    assert f"broker account {BROKER_ACCOUNT_ID}" in result.output
    assert "U9999999" not in result.output
    # Payload deploys the broker account (legacy strings dropped).
    body = capture_request["body"]
    assert body["broker_account_id"] == BROKER_ACCOUNT_ID
    assert "account_id" not in body


def test_start_portfolio_broker_account_id_archived_422_exits_nonzero(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_archived_422_response)
    result = CliRunner().invoke(
        app,
        [
            "live",
            "start-portfolio",
            "--revision",
            REVISION_ID,
            "--broker-account-id",
            BROKER_ACCOUNT_ID,
        ],
    )
    assert result.exit_code != 0
    # the API error body is surfaced (not swallowed)
    assert "422" in result.output
    assert "BROKER_ACCOUNT_ARCHIVED" in result.output


def test_start_portfolio_legacy_account_and_login_key_still_works(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_success_response)
    result = CliRunner().invoke(
        app,
        [
            "live",
            "start-portfolio",
            "--revision",
            REVISION_ID,
            "--account",
            "DU1234567",
            "--ib-login-key",
            "marin1016test",
        ],
    )
    assert result.exit_code == 0, result.output
    body = capture_request["body"]
    assert body["account_id"] == "DU1234567"
    assert body["ib_login_key"] == "marin1016test"
    assert body.get("broker_account_id") in (None, "")
    assert "dep-uuid-1" in result.stdout


# ----------------------------------------------------------------------
# live start (alias — mirrors start-portfolio)
# ----------------------------------------------------------------------


def test_live_start_broker_account_id_only_succeeds(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_success_response)
    result = CliRunner().invoke(
        app,
        [
            "live",
            "start",
            REVISION_ID,
            "--broker-account-id",
            BROKER_ACCOUNT_ID,
        ],
    )
    assert result.exit_code == 0, result.output
    body = capture_request["body"]
    assert body["broker_account_id"] == BROKER_ACCOUNT_ID
    assert body["portfolio_revision_id"] == REVISION_ID
    assert "account_id" not in body or not body["account_id"]
    assert "dep-uuid-1" in result.stdout


def test_live_start_broker_account_id_archived_422_exits_nonzero(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_archived_422_response)
    result = CliRunner().invoke(
        app,
        [
            "live",
            "start",
            REVISION_ID,
            "--broker-account-id",
            BROKER_ACCOUNT_ID,
        ],
    )
    assert result.exit_code != 0
    assert "422" in result.output
    assert "BROKER_ACCOUNT_ARCHIVED" in result.output


def test_live_start_legacy_account_and_login_key_still_works(capture_request) -> None:  # type: ignore[no-untyped-def]
    capture_request["_install"](_success_response)
    result = CliRunner().invoke(
        app,
        [
            "live",
            "start",
            REVISION_ID,
            "DU1234567",
            "--ib-login-key",
            "marin1016test",
        ],
    )
    assert result.exit_code == 0, result.output
    body = capture_request["body"]
    assert body["account_id"] == "DU1234567"
    assert body["ib_login_key"] == "marin1016test"
    assert body.get("broker_account_id") in (None, "")
    assert "dep-uuid-1" in result.stdout


def test_both_commands_expose_broker_account_id_option() -> None:
    import re

    # rich colorizes option names in --help, which in a TTY/CI terminal splits the
    # literal "--broker-account-id" across SGR escape spans (e.g. "--broker" +
    # "-account-id"); locally (no color) it is contiguous. Strip ANSI escapes AND
    # normalize whitespace before matching so the assertion holds in both renders.
    ansi = re.compile(r"\x1b\[[0-9;]*m")

    def _clean(out: str) -> str:
        return " ".join(ansi.sub("", out).split())

    runner = CliRunner()
    sp = runner.invoke(app, ["live", "start-portfolio", "--help"])
    st = runner.invoke(app, ["live", "start", "--help"])
    assert sp.exit_code == 0 and st.exit_code == 0
    assert "broker-account-id" in _clean(sp.stdout)
    assert "broker-account-id" in _clean(st.stdout)
