"""Pydantic schema tests for Symbol Onboarding."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from msai.schemas.symbol_onboarding import (
    OnboardRequest,
    OnboardSymbolSpec,
    SymbolStatus,
    SymbolStepStatus,
)


def _mk_spec(symbol: str = "AAPL", **kwargs) -> OnboardSymbolSpec:
    return OnboardSymbolSpec(
        symbol=symbol,
        asset_class=kwargs.pop("asset_class", "equity"),
        start=kwargs.pop("start", date(2024, 1, 1)),
        end=kwargs.pop("end", date(2024, 12, 31)),
        **kwargs,
    )


def test_request_happy_path():
    req = OnboardRequest(watchlist_name="core", symbols=[_mk_spec()])
    assert req.request_live_qualification is False
    assert req.cost_ceiling_usd is None


def test_request_rejects_empty_symbols():
    with pytest.raises(ValidationError, match="symbols"):
        OnboardRequest(watchlist_name="core", symbols=[])


def test_request_rejects_over_100_symbols():
    with pytest.raises(ValidationError, match="100"):
        OnboardRequest(
            watchlist_name="core",
            symbols=[_mk_spec(f"SYM{i}") for i in range(101)],
        )


def test_symbol_spec_rejects_end_before_start():
    with pytest.raises(ValidationError, match="end must be >= start"):
        OnboardSymbolSpec(
            symbol="AAPL",
            asset_class="equity",
            start=date(2024, 12, 31),
            end=date(2024, 1, 1),
        )


def test_symbol_spec_rejects_future_start():
    from datetime import timedelta

    tomorrow = date.today() + timedelta(days=1)
    with pytest.raises(ValidationError, match="start must be <= today"):
        OnboardSymbolSpec(
            symbol="AAPL",
            asset_class="equity",
            start=tomorrow,
            end=tomorrow,
        )


def test_symbol_spec_rejects_unknown_asset_class():
    with pytest.raises(ValidationError):
        OnboardSymbolSpec(
            symbol="AAPL",
            asset_class="etf",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
        )


def test_symbol_spec_rejects_bad_symbol_regex():
    with pytest.raises(ValidationError):
        OnboardSymbolSpec(
            symbol="AAPL$BAD",
            asset_class="equity",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
        )


def test_cost_ceiling_usd_rejects_negative():
    with pytest.raises(ValidationError):
        OnboardRequest(
            watchlist_name="core",
            symbols=[_mk_spec()],
            cost_ceiling_usd=-1.00,
        )


def test_symbol_status_enum_values():
    assert SymbolStatus.NOT_STARTED == "not_started"
    assert SymbolStatus.IN_PROGRESS == "in_progress"
    assert SymbolStatus.SUCCEEDED == "succeeded"
    assert SymbolStatus.FAILED == "failed"


def test_symbol_step_status_enum_values():
    assert SymbolStepStatus.PENDING == "pending"
    assert SymbolStepStatus.BOOTSTRAP == "bootstrap"
    assert SymbolStepStatus.INGEST == "ingest"
    assert SymbolStepStatus.COVERAGE == "coverage"
    assert SymbolStepStatus.IB_QUALIFY == "ib_qualify"
    assert SymbolStepStatus.COMPLETED == "completed"
    assert SymbolStepStatus.IB_SKIPPED == "ib_skipped"
    assert SymbolStepStatus.COVERAGE_FAILED == "coverage_failed"


def test_cost_ceiling_usd_accepts_decimal_round_trip():
    from decimal import Decimal

    req = OnboardRequest(
        watchlist_name="core",
        symbols=[_mk_spec()],
        cost_ceiling_usd=Decimal("123.45"),
    )
    assert req.cost_ceiling_usd == Decimal("123.45")
