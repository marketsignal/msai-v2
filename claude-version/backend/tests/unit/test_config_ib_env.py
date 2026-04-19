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
