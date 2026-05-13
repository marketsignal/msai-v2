"""Regression tests for Settings — auto-heal knobs + ingest queue name.

Council-locked defaults for backtest auto-ingest on missing data
(2026-04-21). See docs/prds/backtest-auto-ingest-on-missing-data.md §5
(Technical Constraints) and the research brief §1-2 for the math behind
these values.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from msai.core.config import Settings, settings


def test_auto_heal_settings_have_council_defaults() -> None:
    """Council-locked defaults (2026-04-21)."""
    s = Settings()
    assert s.auto_heal_max_years == 10
    assert s.auto_heal_max_symbols == 20
    assert s.auto_heal_allow_options is False
    assert s.auto_heal_wall_clock_cap_seconds == 1800
    assert s.auto_heal_poll_interval_seconds == 10
    assert s.auto_heal_lock_ttl_seconds == 3000
    assert s.ingest_queue_name == "msai:ingest"


@pytest.mark.parametrize(
    ("env_var", "env_value", "attr", "expected"),
    [
        ("INGEST_QUEUE_NAME", "custom:ingest", "ingest_queue_name", "custom:ingest"),
        ("AUTO_HEAL_MAX_YEARS", "5", "auto_heal_max_years", 5),
        ("AUTO_HEAL_MAX_SYMBOLS", "10", "auto_heal_max_symbols", 10),
        ("AUTO_HEAL_ALLOW_OPTIONS", "true", "auto_heal_allow_options", True),
        ("AUTO_HEAL_WALL_CLOCK_CAP_SECONDS", "600", "auto_heal_wall_clock_cap_seconds", 600),
        ("AUTO_HEAL_POLL_INTERVAL_SECONDS", "5", "auto_heal_poll_interval_seconds", 5),
        ("AUTO_HEAL_LOCK_TTL_SECONDS", "1200", "auto_heal_lock_ttl_seconds", 1200),
    ],
)
def test_auto_heal_settings_env_override(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
    env_value: str,
    attr: str,
    expected: object,
) -> None:
    """Each auto-heal setting can be overridden via its env var.

    Covers all 7 fields so a typo in any env alias ships red.
    """
    monkeypatch.setenv(env_var, env_value)
    s = Settings()
    assert getattr(s, attr) == expected


def test_default_cost_ceiling_is_50_usd() -> None:
    """Default symbol-onboarding cost ceiling is $50.00 (Task B4)."""
    assert settings.symbol_onboarding_default_cost_ceiling_usd == Decimal("50.00")


def test_cost_ceiling_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operators can raise/lower the default ceiling via env without redeploy.

    Validates the canonical alias (``MSAI_SYMBOL_ONBOARDING_DEFAULT_COST_CEILING_USD``).
    """
    monkeypatch.setenv("MSAI_SYMBOL_ONBOARDING_DEFAULT_COST_CEILING_USD", "100.50")
    s = Settings()
    assert s.symbol_onboarding_default_cost_ceiling_usd == Decimal("100.50")


def test_ib_port_default_is_socat_proxy_paper_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Settings.ib_port`` must default to 4004 — the gnzsnz socat-proxy port
    for paper trading.

    Regression guard for the 2026-05-12 paper-drill discovery: IB Gateway
    inside the gnzsnz/ib-gateway image binds to ``127.0.0.1:4002`` (paper)
    and accepts API connections ONLY from container-local. A ``socat`` proxy
    listens on ``0.0.0.0:4004`` and re-originates each connection as
    localhost. Cross-container clients (backend, live-supervisor) MUST target
    the socat port; targeting 4002 directly TCP-connects but the API
    handshake times out. See ``ib_port_validator.IB_PAPER_PORTS == (4002, 4004)``.

    Pydantic Settings reads ``.env`` by default; this test disables that
    explicitly so the assertion exercises the Python-level default rather
    than whatever happens to be in the local ``.env``.
    """
    monkeypatch.delenv("IB_PORT", raising=False)
    monkeypatch.delenv("IB_GATEWAY_PORT_PAPER", raising=False)
    s = Settings(_env_file=None)
    assert s.ib_port == 4004, (
        f"Settings.ib_port default must be 4004 (socat paper port); got {s.ib_port}"
    )
