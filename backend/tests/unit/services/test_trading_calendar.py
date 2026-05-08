from __future__ import annotations

from datetime import date

from msai.services.trading_calendar import (
    asset_class_to_exchange,
    trading_days,
)


def test_nyse_excludes_weekends() -> None:
    # 2025-07-04 (Fri) is Independence Day; 2025-07-05 + 06 are weekend.
    days = trading_days(date(2025, 7, 1), date(2025, 7, 7), asset_class="equity")
    assert date(2025, 7, 1) in days  # Tue
    assert date(2025, 7, 2) in days  # Wed
    assert date(2025, 7, 3) in days  # Thu
    assert date(2025, 7, 4) not in days  # holiday
    assert date(2025, 7, 5) not in days  # weekend
    assert date(2025, 7, 6) not in days  # weekend
    assert date(2025, 7, 7) in days  # Mon


def test_cme_for_futures() -> None:
    # CME (Globex) trades through some bank holidays that NYSE closes
    # for (e.g. MLK), so we pick a holiday CMES *does* close for —
    # Christmas 2025-12-25 (Thu) — to assert the wiring routes through
    # the CMES calendar (not the weekday-only fallback, which would
    # include Thursday).
    days = trading_days(date(2025, 12, 23), date(2025, 12, 26), asset_class="futures")
    assert date(2025, 12, 23) in days  # Tue
    assert date(2025, 12, 24) in days  # Wed
    assert date(2025, 12, 25) not in days  # Christmas — CMES closed
    assert date(2025, 12, 26) in days  # Fri


def test_unknown_asset_class_falls_back_to_bdate_range() -> None:
    # crypto: trades 24/7 in reality, but our parquet partition convention
    # is weekday-only; fall-back to bdate_range avoids requiring a calendar.
    days = trading_days(date(2025, 7, 5), date(2025, 7, 7), asset_class="crypto")
    assert date(2025, 7, 5) not in days
    assert date(2025, 7, 6) not in days
    assert date(2025, 7, 7) in days


def test_asset_class_to_exchange_map_is_explicit() -> None:
    # Ingest-taxonomy keys (the canonical input — produced by
    # normalize_asset_class_for_ingest):
    assert asset_class_to_exchange("stocks") == "XNYS"
    assert asset_class_to_exchange("options") == "XNYS"
    assert asset_class_to_exchange("forex") == "XNYS"  # FX OTC 24/5 — NYSE proxy
    assert asset_class_to_exchange("futures") == "CMES"
    assert asset_class_to_exchange("crypto") is None
    # Registry-taxonomy aliases (defensive for tests / ad-hoc scripts):
    assert asset_class_to_exchange("equity") == "XNYS"
    assert asset_class_to_exchange("option") == "XNYS"
    assert asset_class_to_exchange("fx") == "XNYS"


def test_normalize_then_map_for_fx_routes_to_nyse() -> None:
    """End-to-end: a registry-side ``"fx"`` flows through
    ``normalize_asset_class_for_ingest`` → ``"forex"`` → ``XNYS``.
    The mapping must hold for the post-normalize string the API endpoint
    actually passes into ``compute_coverage``."""
    from msai.services.symbol_onboarding import normalize_asset_class_for_ingest

    ingest = normalize_asset_class_for_ingest("fx")
    assert ingest == "forex"
    assert asset_class_to_exchange(ingest) == "XNYS"


def test_trading_days_inclusive_of_both_endpoints() -> None:
    days = trading_days(date(2025, 7, 1), date(2025, 7, 1), asset_class="equity")
    assert days == {date(2025, 7, 1)}


def test_trading_days_empty_when_start_after_end() -> None:
    days = trading_days(date(2025, 7, 5), date(2025, 7, 1), asset_class="equity")
    assert days == set()
