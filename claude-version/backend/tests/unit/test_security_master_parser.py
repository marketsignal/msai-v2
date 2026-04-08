"""Unit tests for :func:`extract_trading_hours` and
:func:`nautilus_instrument_to_cache_json` (Phase 2 task 2.4)."""

from __future__ import annotations

from msai.services.nautilus.security_master.parser import (
    _parse_ib_hours_string,
    extract_trading_hours,
)

# ---------------------------------------------------------------------------
# _parse_ib_hours_string
# ---------------------------------------------------------------------------


class TestParseIbHoursString:
    def test_empty_string_returns_empty_list(self) -> None:
        assert _parse_ib_hours_string("") == []

    def test_single_session(self) -> None:
        """One session, standard format."""
        # 2026-06-01 is a Monday
        result = _parse_ib_hours_string("20260601:0930-20260601:1600")
        assert result == [{"day": "MON", "open": "09:30", "close": "16:00"}]

    def test_closed_day_skipped(self) -> None:
        """``CLOSED`` sessions do NOT produce dict entries."""
        result = _parse_ib_hours_string("20260601:0930-20260601:1600;20260525:CLOSED")
        assert len(result) == 1
        assert result[0]["day"] == "MON"

    def test_multi_day_deduplication(self) -> None:
        """Five days of NYSE RTH with identical open/close → one
        entry per distinct weekday."""
        # 2026-06-01 Mon through 2026-06-05 Fri
        hours = ";".join(f"2026060{i}:0930-2026060{i}:1600" for i in range(1, 6))
        result = _parse_ib_hours_string(hours)
        days = [s["day"] for s in result]
        assert days == ["MON", "TUE", "WED", "THU", "FRI"]
        for s in result:
            assert s["open"] == "09:30"
            assert s["close"] == "16:00"

    def test_identical_sessions_dedup(self) -> None:
        """If IB repeats the same weekday-open-close tuple across
        multiple dates (e.g. ETH sessions spanning weeks), only
        emit one entry per distinct tuple."""
        hours = (
            "20260601:0400-20260601:2000;"  # Mon 04:00-20:00
            "20260608:0400-20260608:2000"  # Mon 04:00-20:00 (again)
        )
        result = _parse_ib_hours_string(hours)
        assert len(result) == 1
        assert result[0] == {"day": "MON", "open": "04:00", "close": "20:00"}

    def test_malformed_entry_is_skipped(self) -> None:
        """An unparseable entry (e.g. garbage format) is skipped
        silently — we prefer partial data over failing the whole
        cache write."""
        hours = "NOT_A_DATE:lol-bad;20260601:0930-20260601:1600"
        result = _parse_ib_hours_string(hours)
        assert len(result) == 1
        assert result[0]["day"] == "MON"


# ---------------------------------------------------------------------------
# extract_trading_hours
# ---------------------------------------------------------------------------


class TestExtractTradingHours:
    def test_nyse_equity_rth_and_eth(self) -> None:
        """Equity with both RTH and ETH sessions. The ``eth`` array
        is derived as ``trading_hours - liquid_hours``."""
        trading_hours = (
            "20260601:0400-20260601:2000;"  # Mon pre/post market
            "20260601:0930-20260601:1600"  # Mon RTH
        )
        liquid_hours = "20260601:0930-20260601:1600"

        result = extract_trading_hours(
            trading_hours=trading_hours,
            liquid_hours=liquid_hours,
            time_zone_id="America/New_York",
        )

        assert result is not None
        assert result["timezone"] == "America/New_York"
        assert result["rth"] == [{"day": "MON", "open": "09:30", "close": "16:00"}]
        assert result["eth"] == [{"day": "MON", "open": "04:00", "close": "20:00"}]

    def test_returns_none_when_both_strings_empty(self) -> None:
        """24h venues (crypto, some forex) have no meaningful
        session structure — return ``None`` so the cache row stores
        NULL in the ``trading_hours`` column."""
        result = extract_trading_hours(
            trading_hours=None,
            liquid_hours=None,
            time_zone_id="UTC",
        )
        assert result is None

    def test_returns_none_when_both_strings_all_closed(self) -> None:
        """Only CLOSED entries → no extractable sessions → ``None``."""
        result = extract_trading_hours(
            trading_hours="20260101:CLOSED",
            liquid_hours="20260101:CLOSED",
            time_zone_id="America/New_York",
        )
        assert result is None

    def test_timezone_defaults_to_utc_when_missing(self) -> None:
        """Defensive default — some contracts don't report a
        ``timeZoneId``."""
        result = extract_trading_hours(
            trading_hours="20260601:0930-20260601:1600",
            liquid_hours="20260601:0930-20260601:1600",
            time_zone_id=None,
        )
        assert result is not None
        assert result["timezone"] == "UTC"

    def test_futures_ccl_near_24h(self) -> None:
        """CME ES trades nearly 24h on weekdays with a short daily
        break. The extractor should produce distinct MON-FRI
        sessions."""
        # Simplified CME schedule: Mon-Fri 17:00 previous day → 16:00
        # next day. We use a simpler single-day-each shape here
        # because the extractor's weekday-deduplication logic is
        # what we're testing.
        trading_hours = (
            "20260601:1700-20260602:1600;"  # Mon-Tue
            "20260602:1700-20260603:1600;"  # Tue-Wed
            "20260603:1700-20260604:1600"  # Wed-Thu
        )
        liquid_hours = "20260601:1700-20260602:1600"

        result = extract_trading_hours(
            trading_hours=trading_hours,
            liquid_hours=liquid_hours,
            time_zone_id="America/Chicago",
        )
        assert result is not None
        assert result["timezone"] == "America/Chicago"
        # The parser records the OPEN date's weekday. Mon+Tue+Wed opens.
        days_in_rth_or_eth = {s["day"] for s in result["rth"] + result["eth"]}
        assert days_in_rth_or_eth == {"MON", "TUE", "WED"}
