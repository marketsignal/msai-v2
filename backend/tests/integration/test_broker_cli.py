"""Integration tests for the ``msai broker`` CLI sub-app (Task 13).

These tests assert the wiring + credential-handling discipline of the broker
sub-app:

- the sub-app is registered with the documented commands;
- ``add`` reads the TWS password from ``$MSAI_BROKER_TWS_PASSWORD`` (never argv)
  and sends it in the request body, but never echoes it to stdout;
- there is no ``--tws-password`` flag at all (argv leaks via shell history + ps).

Full end-to-end behaviour against the real API is covered by the CLI E2E UC;
here we monkeypatch ``_api_call`` so the tests run without a live backend.
"""

from __future__ import annotations

import httpx
from typer.testing import CliRunner

from msai.cli import app


def test_broker_subapp_registered() -> None:
    result = CliRunner().invoke(app, ["broker", "--help"])
    assert result.exit_code == 0
    assert "add" in result.stdout and "list" in result.stdout


def test_broker_add_takes_password_from_env_not_argv(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Codex iter-1 P0#2: password NEVER on argv (shell history / `ps` leak). It comes from
    # MSAI_BROKER_TWS_PASSWORD (scriptable) or an interactive getpass prompt.
    # Codex iter-1 P2#9: _api_call signature is (method, path, *, json_body=..., ...) -> Response.
    captured: dict[str, object] = {}

    def fake_api_call(method, path, *, json_body=None, **kw):  # type: ignore[no-untyped-def]
        captured["body"] = json_body
        return httpx.Response(
            201,
            json={
                "id": "uuid-1",
                "ib_account_id": "DU1",
                "status": "active",
                "gateway_slot": "slot-a",
            },
            request=httpx.Request(method, f"http://test{path}"),
        )

    monkeypatch.setattr("msai.cli._api_call", fake_api_call)
    monkeypatch.setenv("MSAI_BROKER_TWS_PASSWORD", "secretpw")
    result = CliRunner().invoke(
        app,
        [
            "broker",
            "add",
            "--ib-account-id",
            "DU1",
            "--ib-login-key",
            "L1",
            "--trading-mode",
            "paper",
            "--tws-userid",
            "u",
        ],
    )
    assert result.exit_code == 0
    assert "secretpw" not in result.stdout  # never echo the password
    assert "DU1" in result.stdout
    assert captured["body"]["tws_password"] == "secretpw"  # type: ignore[index]  # but it IS sent to the API body


def test_broker_add_has_no_password_flag() -> None:
    # the flag must not exist at all — no --tws-password on argv, ever
    result = CliRunner().invoke(app, ["broker", "add", "--help"])
    assert "--tws-password" not in result.stdout
