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
