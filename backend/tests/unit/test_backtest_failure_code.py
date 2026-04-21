"""Tests for the backtest failure code enum."""

from __future__ import annotations

from msai.services.backtests.failure_code import FailureCode


def test_failure_code_members_match_prd():
    # [iter-1] Dropped MISSING_STRATEGY_DATA_FOR_PERIOD — empty bars is a
    # 0-trade success in BacktestRunner, not a failure. Codex P1-c.
    # [iter-3] Dropped CONFIG_REJECTED_AT_WORKER — no emission path today.
    assert {c.value for c in FailureCode} == {
        "missing_data",
        "strategy_import_error",
        "engine_crash",
        "timeout",
        "unknown",
    }


def test_parse_or_unknown_accepts_none():
    assert FailureCode.parse_or_unknown(None) is FailureCode.UNKNOWN


def test_parse_or_unknown_accepts_unknown_string():
    # Historical rows may carry values not in the current enum —
    # those must degrade to UNKNOWN, not raise.
    assert FailureCode.parse_or_unknown("historical_legacy_code") is FailureCode.UNKNOWN


def test_parse_or_unknown_accepts_real_value():
    assert FailureCode.parse_or_unknown("missing_data") is FailureCode.MISSING_DATA
