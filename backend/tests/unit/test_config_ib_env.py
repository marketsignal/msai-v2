"""Regression for IB Gateway env-var drift (drill 2026-04-15).

The Docker compose sets ``IB_GATEWAY_HOST`` + ``IB_GATEWAY_PORT_PAPER``
on every service that connects to IB. pydantic-settings reads a field
named ``ib_host`` from env var ``IB_HOST`` (case-insensitive) by
default, so the compose variables were ignored and the backend fell
back to ``127.0.0.1:4002`` — wrong host (not reachable from the
backend container) and wrong port (internal paper port, not the
socat-forwarded external port). Symptom: ``/api/v1/account/health``
always reported ``gateway_connected=false`` even when IB Gateway was
healthy.

Fix: accept either env-var naming via ``AliasChoices`` so both the
legacy ``IB_HOST``/``IB_PORT`` (used in unit tests / local dev) and
the compose-native ``IB_GATEWAY_HOST``/``IB_GATEWAY_PORT_PAPER`` land
in ``settings.ib_host`` / ``settings.ib_port``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_ib_host_picked_up_from_ib_gateway_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose sets ``IB_GATEWAY_HOST=ib-gateway``; settings must see it."""
    monkeypatch.setenv("IB_GATEWAY_HOST", "ib-gateway")
    monkeypatch.delenv("IB_HOST", raising=False)

    from msai.core.config import Settings

    settings = Settings()
    assert settings.ib_host == "ib-gateway"


def test_ib_port_picked_up_from_ib_gateway_port_paper_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose sets ``IB_GATEWAY_PORT_PAPER=4004`` (socat paper endpoint);
    settings must see it as ``ib_port``."""
    monkeypatch.setenv("IB_GATEWAY_PORT_PAPER", "4004")
    monkeypatch.delenv("IB_PORT", raising=False)

    from msai.core.config import Settings

    settings = Settings()
    assert settings.ib_port == 4004


def test_legacy_ib_host_env_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests and local dev set ``IB_HOST`` directly; the alias
    must not break that path."""
    monkeypatch.setenv("IB_HOST", "10.0.0.5")
    monkeypatch.delenv("IB_GATEWAY_HOST", raising=False)

    from msai.core.config import Settings

    settings = Settings()
    assert settings.ib_host == "10.0.0.5"


def test_legacy_ib_port_env_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests and local dev set ``IB_PORT`` directly; the alias
    must not break that path."""
    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.delenv("IB_GATEWAY_PORT_PAPER", raising=False)

    from msai.core.config import Settings

    settings = Settings()
    assert settings.ib_port == 4002


def test_explicit_ib_host_wins_over_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both ``IB_HOST`` and ``IB_GATEWAY_HOST`` are set, the
    primary name wins. This keeps operator overrides deterministic:
    set ``IB_HOST`` to override compose defaults without editing
    compose files."""
    monkeypatch.setenv("IB_HOST", "override.example.com")
    monkeypatch.setenv("IB_GATEWAY_HOST", "ib-gateway")

    from msai.core.config import Settings

    settings = Settings()
    assert settings.ib_host == "override.example.com"


def test_ib_connect_timeout_seconds_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh instance reads IB_CONNECT_TIMEOUT_SECONDS alias; defaults to 5."""
    from msai.core.config import Settings

    monkeypatch.delenv("IB_CONNECT_TIMEOUT_SECONDS", raising=False)
    assert Settings().ib_connect_timeout_seconds == 5
    monkeypatch.setenv("IB_CONNECT_TIMEOUT_SECONDS", "12")
    assert Settings().ib_connect_timeout_seconds == 12


def test_ib_request_timeout_seconds_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh instance reads IB_REQUEST_TIMEOUT_SECONDS alias; defaults to 30."""
    from msai.core.config import Settings

    monkeypatch.delenv("IB_REQUEST_TIMEOUT_SECONDS", raising=False)
    assert Settings().ib_request_timeout_seconds == 30
    monkeypatch.setenv("IB_REQUEST_TIMEOUT_SECONDS", "60")
    assert Settings().ib_request_timeout_seconds == 60


def test_ib_instrument_client_id_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh instance reads IB_INSTRUMENT_CLIENT_ID alias; defaults to 999."""
    from msai.core.config import Settings

    monkeypatch.delenv("IB_INSTRUMENT_CLIENT_ID", raising=False)
    assert Settings().ib_instrument_client_id == 999
    monkeypatch.setenv("IB_INSTRUMENT_CLIENT_ID", "900")
    assert Settings().ib_instrument_client_id == 900


def test_azure_keyvault_uri_and_mi_client_id_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AZURE_KEYVAULT_URI`` / ``AZURE_KV_MI_CLIENT_ID`` are declared Settings
    fields and load from env without raising.

    Regression for the iter-5 P2-1 startup crash: operators are told to put
    ``AZURE_KEYVAULT_URI=`` in ``.env`` (see ``.env.example``), but the fields
    were not declared on ``Settings``. pydantic-settings defaults to
    ``extra="forbid"``, so an undeclared key sourced from a ``.env`` file raised
    ``ValidationError: extra_forbidden`` at import — the backend would not boot.
    """
    from msai.core.config import Settings

    monkeypatch.setenv("AZURE_KEYVAULT_URI", "https://kv.example.vault.azure.net/")
    monkeypatch.setenv("AZURE_KV_MI_CLIENT_ID", "00000000-0000-0000-0000-000000000001")
    settings = Settings()
    assert settings.azure_keyvault_uri == "https://kv.example.vault.azure.net/"
    assert settings.azure_kv_mi_client_id == "00000000-0000-0000-0000-000000000001"


def test_azure_keyvault_fields_default_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both KV fields default to ``None`` when unset (dev: file-backed store)."""
    from msai.core.config import Settings

    monkeypatch.delenv("AZURE_KEYVAULT_URI", raising=False)
    monkeypatch.delenv("AZURE_KV_MI_CLIENT_ID", raising=False)
    settings = Settings()
    assert settings.azure_keyvault_uri is None
    assert settings.azure_kv_mi_client_id is None


def test_dotenv_broker_extras_do_not_crash_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``.env`` file containing the broker env vars operators are told to set
    does not crash ``Settings()`` with ``extra_forbidden``.

    pydantic-settings forbids extras sourced from a ``.env`` file (env-var
    extras pass through, but dotenv-file extras are validated against the
    model). ``.env.example`` documents ``AZURE_KEYVAULT_URI``,
    ``AZURE_KV_MI_CLIENT_ID``, and ``BROKER_ACCOUNT_BACKFILL`` — all three MUST
    be declared on ``Settings`` so a populated ``.env`` boots the backend.
    """
    from msai.core.config import Settings

    # Clear inherited env so the .env file is the only source for these keys.
    for key in ("AZURE_KEYVAULT_URI", "AZURE_KV_MI_CLIENT_ID", "BROKER_ACCOUNT_BACKFILL"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZURE_KEYVAULT_URI=https://kv.example.vault.azure.net/\n"
        "AZURE_KV_MI_CLIENT_ID=00000000-0000-0000-0000-000000000002\n"
        "BROKER_ACCOUNT_BACKFILL=U4715997:hvp:ib-gateway:live:TWS_USERID|TWS_PASSWORD\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]

    assert settings.azure_keyvault_uri == "https://kv.example.vault.azure.net/"
    assert settings.azure_kv_mi_client_id == "00000000-0000-0000-0000-000000000002"
    assert (
        settings.broker_account_backfill == "U4715997:hvp:ib-gateway:live:TWS_USERID|TWS_PASSWORD"
    )
