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


def test_broker_add_sends_account_class(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Task 6 iter-4 P2: --account-class (paper|test|real) reaches the create
    # payload so the fund can be registered as `real` from the CLI, not only the
    # UI wizard (Task 12). The schema defaults it from trading_mode when omitted.
    captured: dict[str, object] = {}

    def fake_api_call(method, path, *, json_body=None, **kw):  # type: ignore[no-untyped-def]
        captured["body"] = json_body
        return httpx.Response(
            201,
            json={
                "id": "uuid-1",
                "ib_account_id": "U4715997",
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
            "U4715997",
            "--ib-login-key",
            "L1",
            "--trading-mode",
            "live",
            "--tws-userid",
            "u",
            "--account-class",
            "real",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert captured["body"]["account_class"] == "real"  # type: ignore[index]


def test_broker_add_omits_account_class_when_not_given(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Without --account-class the key is absent — the server defaults it from
    # trading_mode (Task 3), so the CLI must not send a guessed value.
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
    assert result.exit_code == 0, result.stdout
    assert "account_class" not in captured["body"]  # type: ignore[operator]


def test_broker_list_shows_account_class_column(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Task 6 iter-4 P2: `broker list` must surface account_class so the operator
    # can see which registered account is the fund (`real`) vs a test/paper one.
    def fake_api_call(method, path, *, json_body=None, **kw):  # type: ignore[no-untyped-def]
        return httpx.Response(
            200,
            json=[
                {
                    "id": "uuid-1",
                    "ib_account_id": "U4715997",
                    "status": "active",
                    "trading_mode": "live",
                    "account_class": "real",
                    "gateway_slot": "slot-a",
                    "credentials_secret_version": "v1",
                },
            ],
            request=httpx.Request(method, f"http://test{path}"),
        )

    monkeypatch.setattr("msai.cli._api_call", fake_api_call)
    result = CliRunner().invoke(app, ["broker", "list"])
    assert result.exit_code == 0, result.stdout
    # column header + the row value both visible (CLASS header avoids colliding
    # with the existing MODE column for trading_mode).
    assert "CLASS" in result.stdout
    assert "real" in result.stdout
