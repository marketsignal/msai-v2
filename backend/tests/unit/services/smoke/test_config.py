"""Unit tests for the canonical smoke configurations."""

from __future__ import annotations

from datetime import date

import pytest

from msai.services.smoke.config import SMOKE_FAST, SMOKE_NIGHTLY, SmokeConfig


def test_smoke_fast_two_equity_symbols_and_one_month_window() -> None:
    # Act / Assert
    assert isinstance(SMOKE_FAST, SmokeConfig)
    assert SMOKE_FAST.name == "fast"
    assert tuple(SMOKE_FAST.symbols) == ("AAPL", "SPY")
    assert SMOKE_FAST.start_date == date(2024, 12, 1)
    assert SMOKE_FAST.end_date == date(2024, 12, 31)
    assert SMOKE_FAST.benchmark_symbol == "SPY"
    assert SMOKE_FAST.strategy_names == (
        "__smoke__/smoke_market_order/AAPL",
        "__smoke__/smoke_market_order/SPY",
        "__smoke__/ema_cross/AAPL",
        "__smoke__/ema_cross/SPY",
    )


def test_smoke_nightly_two_equity_symbols_and_full_year_window() -> None:
    assert SMOKE_NIGHTLY.name == "nightly"
    assert tuple(SMOKE_NIGHTLY.symbols) == ("AAPL", "SPY")
    assert SMOKE_NIGHTLY.start_date == date(2024, 1, 1)
    assert SMOKE_NIGHTLY.end_date == date(2024, 12, 31)


def test_smoke_config_is_frozen() -> None:
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        SMOKE_FAST.symbols = ()  # type: ignore[misc]
