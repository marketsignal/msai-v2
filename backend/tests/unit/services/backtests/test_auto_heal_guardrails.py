"""Unit tests for the auto-heal guardrail evaluator."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from msai.services.backtests.auto_heal_guardrails import (
    GuardrailResult,
    evaluate_guardrails,
)


def _g(**overrides: Any) -> GuardrailResult:
    base: dict[str, Any] = {
        "asset_class": "stocks",
        "symbols": ["AAPL"],
        "start": date(2024, 1, 1),
        "end": date(2024, 12, 31),
        "max_years": 10,
        "max_symbols": 20,
        "allow_options": False,
    }
    base.update(overrides)
    return evaluate_guardrails(**base)


def test_happy_path_within_all_caps() -> None:
    result = _g()
    assert result.allowed is True
    assert result.reason is None


def test_rejects_options_asset_class() -> None:
    result = _g(asset_class="options")
    assert result.allowed is False
    assert result.reason == "options_disabled"
    assert "options" in result.human_message.lower()


def test_allows_options_when_explicitly_enabled() -> None:
    result = _g(asset_class="options", allow_options=True)
    assert result.allowed is True


def test_rejects_excessive_date_range() -> None:
    result = _g(start=date(2010, 1, 1), end=date(2024, 12, 31))  # ~15y
    assert result.allowed is False
    assert result.reason == "range_exceeds_max_years"
    assert "15" in result.human_message


def test_accepts_exactly_10_years() -> None:
    result = _g(start=date(2014, 1, 1), end=date(2023, 12, 31))
    assert result.allowed is True


def test_rejects_excessive_symbol_count() -> None:
    result = _g(symbols=[f"SYM{i}" for i in range(25)])
    assert result.allowed is False
    assert result.reason == "symbol_count_exceeds_max"


def test_accepts_exactly_max_symbols() -> None:
    result = _g(symbols=[f"SYM{i}" for i in range(20)])
    assert result.allowed is True


def test_empty_symbols_rejected() -> None:
    result = _g(symbols=[])
    assert result.allowed is False
    assert result.reason == "no_symbols"


def test_guardrail_result_rejects_allowed_with_reason() -> None:
    """__post_init__ enforces the allowed/reason pairing invariant."""
    # allowed=True must have reason=None
    with pytest.raises(ValueError, match="allowed=True must have reason=None"):
        GuardrailResult(
            allowed=True,
            reason="options_disabled",
            human_message="inconsistent",
        )
    # allowed=False must have a reason
    with pytest.raises(ValueError, match="allowed=False must have a reason"):
        GuardrailResult(
            allowed=False,
            reason=None,
            human_message="inconsistent",
        )
