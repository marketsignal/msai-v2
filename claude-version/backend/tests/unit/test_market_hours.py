"""Unit tests for the MarketHoursService (Phase 4 task 4.3).

We test the pure window-evaluation logic without standing up
a real DB. The cache primer is a thin DB read that's covered
by an integration test in a follow-up task.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from msai.services.nautilus.market_hours import (
    MarketHoursService,
    make_market_hours_check,
)


def _us_equity_hours() -> dict[str, Any]:
    """Schedule for a US equity (RTH 09:30-16:00 ET, ETH 04:00-20:00 ET)."""
    return {
        "timezone": "America/New_York",
        "rth": [
            {"day": "MON", "open": "09:30", "close": "16:00"},
            {"day": "TUE", "open": "09:30", "close": "16:00"},
            {"day": "WED", "open": "09:30", "close": "16:00"},
            {"day": "THU", "open": "09:30", "close": "16:00"},
            {"day": "FRI", "open": "09:30", "close": "16:00"},
        ],
        "eth": [
            {"day": "MON", "open": "04:00", "close": "20:00"},
            {"day": "TUE", "open": "04:00", "close": "20:00"},
            {"day": "WED", "open": "04:00", "close": "20:00"},
            {"day": "THU", "open": "04:00", "close": "20:00"},
            {"day": "FRI", "open": "04:00", "close": "20:00"},
        ],
    }


def _futures_hours() -> dict[str, Any]:
    """Schedule for ESM5 (CME futures — almost 24h with a brief
    daily settlement break, simplified here as 18:00-17:00 ET
    rolling)."""
    return {
        "timezone": "America/Chicago",
        "rth": [
            # CME calls almost everything ETH; RTH is the
            # 09:30-16:00 NY equity window mapped to Chicago
            {"day": "MON", "open": "08:30", "close": "15:00"},
            {"day": "TUE", "open": "08:30", "close": "15:00"},
        ],
        "eth": [
            {"day": "MON", "open": "00:00", "close": "23:59"},
            {"day": "TUE", "open": "00:00", "close": "23:59"},
        ],
    }


# ----------------------------------------------------------------------
# Pure window evaluation
# ----------------------------------------------------------------------


def test_aapl_at_10am_eastern_is_in_rth() -> None:
    service = MarketHoursService()
    service._cache["AAPL.NASDAQ"] = _us_equity_hours()  # noqa: SLF001
    # 10 AM Eastern on a Tuesday (2026-04-07 was a Tuesday)
    ts = datetime(2026, 4, 7, 14, 0, tzinfo=UTC)  # 14:00 UTC = 10:00 EDT
    assert service.is_in_rth("AAPL.NASDAQ", ts) is True


def test_aapl_at_3am_eastern_is_outside_rth() -> None:
    service = MarketHoursService()
    service._cache["AAPL.NASDAQ"] = _us_equity_hours()  # noqa: SLF001
    # 3 AM EDT on a Tuesday = 07:00 UTC
    ts = datetime(2026, 4, 7, 7, 0, tzinfo=UTC)
    assert service.is_in_rth("AAPL.NASDAQ", ts) is False


def test_aapl_weekend_is_outside_rth() -> None:
    service = MarketHoursService()
    service._cache["AAPL.NASDAQ"] = _us_equity_hours()  # noqa: SLF001
    # Saturday at noon EDT
    ts = datetime(2026, 4, 11, 16, 0, tzinfo=UTC)
    assert service.is_in_rth("AAPL.NASDAQ", ts) is False


def test_aapl_at_5am_eastern_is_in_eth() -> None:
    service = MarketHoursService()
    service._cache["AAPL.NASDAQ"] = _us_equity_hours()  # noqa: SLF001
    # 05:00 EDT Tuesday = 09:00 UTC
    ts = datetime(2026, 4, 7, 9, 0, tzinfo=UTC)
    assert service.is_in_eth("AAPL.NASDAQ", ts) is True


def test_es_futures_overnight_is_in_eth() -> None:
    service = MarketHoursService()
    service._cache["ESM5.CME"] = _futures_hours()  # noqa: SLF001
    # 03:00 Chicago time Tuesday = 08:00 UTC
    ts = datetime(2026, 4, 7, 8, 0, tzinfo=UTC)
    assert service.is_in_eth("ESM5.CME", ts) is True


def test_cross_midnight_window_today_match() -> None:
    """Codex batch 10 P2 regression: a session that opens
    at 18:00 today and closes at 01:00 tomorrow must match
    a 19:00-today timestamp via the today-side branch."""
    service = MarketHoursService()
    service._cache["ESM5.CME"] = {  # noqa: SLF001
        "timezone": "America/Chicago",
        "rth": [],
        "eth": [
            # Sunday session: 18:00 → next-day 01:00
            {"day": "SUN", "open": "18:00", "close": "01:00"},
        ],
    }
    # Sunday 19:00 Chicago = Monday 01:00 UTC
    ts = datetime(2026, 4, 13, 0, 0, tzinfo=UTC)  # Sunday 19:00 Chicago
    assert service.is_in_eth("ESM5.CME", ts) is True


def test_cross_midnight_window_post_midnight_match() -> None:
    """The post-midnight tail of yesterday's session must
    match: at 00:30 on Monday, we're still inside Sunday's
    18:00 → 01:00 window via the yesterday-side branch."""
    service = MarketHoursService()
    service._cache["ESM5.CME"] = {  # noqa: SLF001
        "timezone": "America/Chicago",
        "rth": [],
        "eth": [
            {"day": "SUN", "open": "18:00", "close": "01:00"},
        ],
    }
    # Monday 00:30 Chicago = Monday 05:30 UTC (CDT in April)
    ts = datetime(2026, 4, 13, 5, 30, tzinfo=UTC)
    assert service.is_in_eth("ESM5.CME", ts) is True


def test_cross_midnight_window_outside_match() -> None:
    """Same cross-midnight window — at 02:00 Monday, we're
    PAST the close=01:00 boundary, so the window must NOT
    match."""
    service = MarketHoursService()
    service._cache["ESM5.CME"] = {  # noqa: SLF001
        "timezone": "America/Chicago",
        "rth": [],
        "eth": [
            {"day": "SUN", "open": "18:00", "close": "01:00"},
        ],
    }
    # Monday 02:00 Chicago = Monday 07:00 UTC (CDT in April)
    ts = datetime(2026, 4, 13, 7, 0, tzinfo=UTC)
    assert service.is_in_eth("ESM5.CME", ts) is False


# ----------------------------------------------------------------------
# Fail-open semantics
# ----------------------------------------------------------------------


def test_unknown_instrument_treated_as_always_open() -> None:
    """Better to let the order through than to halt every
    instrument with no metadata."""
    service = MarketHoursService()
    ts = datetime(2026, 4, 7, 7, 0, tzinfo=UTC)
    assert service.is_in_rth("UNKNOWN.XYZ", ts) is True


def test_null_trading_hours_treated_as_always_open() -> None:
    """Forex on a 24h venue or continuous futures may have
    NULL trading_hours in the cache. The reader treats this
    as always open."""
    service = MarketHoursService()
    service._cache["EURUSD.IDEALPRO"] = None  # noqa: SLF001
    ts = datetime(2026, 4, 12, 7, 0, tzinfo=UTC)  # Sunday
    assert service.is_in_rth("EURUSD.IDEALPRO", ts) is True


def test_empty_window_list_treated_as_always_open() -> None:
    service = MarketHoursService()
    service._cache["NOSCHED.XYZ"] = {  # noqa: SLF001
        "timezone": "America/New_York",
        "rth": [],
        "eth": [],
    }
    ts = datetime(2026, 4, 7, 14, 0, tzinfo=UTC)
    assert service.is_in_rth("NOSCHED.XYZ", ts) is True


def test_unknown_timezone_falls_open() -> None:
    """A bad timezone string in the cache row should NOT
    crash the strategy. The reader logs a warning and
    treats the instrument as always open."""
    service = MarketHoursService()
    service._cache["BAD.TZ"] = {  # noqa: SLF001
        "timezone": "Mars/Olympus_Mons",
        "rth": [{"day": "MON", "open": "09:30", "close": "16:00"}],
        "eth": [],
    }
    ts = datetime(2026, 4, 7, 14, 0, tzinfo=UTC)
    assert service.is_in_rth("BAD.TZ", ts) is True


# ----------------------------------------------------------------------
# RiskAwareStrategy callable factory
# ----------------------------------------------------------------------


def test_make_market_hours_check_default_uses_rth() -> None:
    service = MarketHoursService()
    service._cache["AAPL.NASDAQ"] = _us_equity_hours()  # noqa: SLF001
    check = make_market_hours_check(service)

    class FakeInstrumentId:
        def __str__(self) -> str:
            return "AAPL.NASDAQ"

    instrument_id = FakeInstrumentId()

    # We can't fix "now" easily without a clock dep, so just
    # verify the function returns a bool without raising.
    result = check(instrument_id)
    assert isinstance(result, bool)


def test_make_market_hours_check_allow_eth_uses_eth() -> None:
    """allow_eth=True should query is_in_eth, not is_in_rth.
    We patch the service methods to record which one was
    called."""
    service = MarketHoursService()
    service._cache["AAPL.NASDAQ"] = _us_equity_hours()  # noqa: SLF001

    rth_calls: list[str] = []
    eth_calls: list[str] = []

    def fake_rth(canonical_id: str, ts: datetime) -> bool:
        rth_calls.append(canonical_id)
        return True

    def fake_eth(canonical_id: str, ts: datetime) -> bool:
        eth_calls.append(canonical_id)
        return True

    service.is_in_rth = fake_rth  # type: ignore[method-assign]
    service.is_in_eth = fake_eth  # type: ignore[method-assign]

    check = make_market_hours_check(service, allow_eth=True)

    class FakeInstrumentId:
        def __str__(self) -> str:
            return "AAPL.NASDAQ"

    instrument_id = FakeInstrumentId()
    check(instrument_id)

    assert eth_calls == ["AAPL.NASDAQ"]
    assert rth_calls == []


def test_make_market_hours_check_default_uses_rth_explicit() -> None:
    service = MarketHoursService()
    service._cache["AAPL.NASDAQ"] = _us_equity_hours()  # noqa: SLF001

    rth_calls: list[str] = []
    eth_calls: list[str] = []

    def fake_rth(canonical_id: str, ts: datetime) -> bool:
        rth_calls.append(canonical_id)
        return True

    def fake_eth(canonical_id: str, ts: datetime) -> bool:
        eth_calls.append(canonical_id)
        return True

    service.is_in_rth = fake_rth  # type: ignore[method-assign]
    service.is_in_eth = fake_eth  # type: ignore[method-assign]

    check = make_market_hours_check(service)  # default allow_eth=False

    class FakeInstrumentId:
        def __str__(self) -> str:
            return "AAPL.NASDAQ"

    instrument_id = FakeInstrumentId()
    check(instrument_id)

    assert rth_calls == ["AAPL.NASDAQ"]
    assert eth_calls == []
