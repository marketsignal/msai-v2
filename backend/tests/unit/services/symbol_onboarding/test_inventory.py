"""Unit tests for inventory status derivation and trailing-only detection."""

from __future__ import annotations

from datetime import date

from msai.services.symbol_onboarding.inventory import (
    derive_status,
    is_trailing_only,
)

TODAY = date(2026, 5, 1)  # Fri — last 7 trading days are Apr 23..May 1.


class TestDeriveStatus:
    def test_not_registered_when_reg_false(self) -> None:
        assert (
            derive_status(
                registered=False,
                bt_avail=False,
                live=False,
                coverage_status=None,
                missing_ranges=[],
                today=TODAY,
            )
            == "not_registered"
        )

    def test_ready_when_full_coverage_plus_live(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=True,
                coverage_status="full",
                missing_ranges=[],
                today=TODAY,
            )
            == "ready"
        )

    def test_backtest_only_when_data_full_no_live(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=False,
                coverage_status="full",
                missing_ranges=[],
                today=TODAY,
            )
            == "backtest_only"
        )

    def test_live_only_when_qualified_no_data(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=False,
                live=True,
                coverage_status="none",
                missing_ranges=[],
                today=TODAY,
            )
            == "live_only"
        )

    def test_gapped_when_mid_window_missing(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=True,
                coverage_status="gapped",
                missing_ranges=[(date(2024, 3, 1), date(2024, 3, 31))],
                today=TODAY,
            )
            == "gapped"
        )

    def test_stale_when_only_trailing_range_missing(self) -> None:
        # Range start 2026-04-28 (Tue) is within last 7 trading days of TODAY.
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=True,
                coverage_status="gapped",
                missing_ranges=[(date(2026, 4, 28), date(2026, 4, 30))],
                today=TODAY,
            )
            == "stale"
        )

    def test_gapped_wins_over_stale_when_both_present(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=True,
                coverage_status="gapped",
                missing_ranges=[
                    (date(2024, 3, 1), date(2024, 3, 31)),
                    (date(2026, 4, 28), date(2026, 4, 30)),
                ],
                today=TODAY,
            )
            == "gapped"
        )

    def test_priority_order_data_beats_registration(self) -> None:
        # Range start 2026-04-28 (Tue) is within last 7 trading days of TODAY.
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=False,
                coverage_status="gapped",
                missing_ranges=[(date(2026, 4, 28), date(2026, 4, 30))],
                today=TODAY,
            )
            == "stale"
        )

    def test_registered_no_data_no_live_is_backtest_only(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=False,
                live=False,
                coverage_status="none",
                missing_ranges=[],
                today=TODAY,
            )
            == "backtest_only"
        )

    def test_registered_full_no_live_is_backtest_only(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=False,
                coverage_status="full",
                missing_ranges=[],
                today=TODAY,
            )
            == "backtest_only"
        )

    def test_long_multi_month_trailing_is_gapped_not_stale(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=True,
                coverage_status="gapped",
                missing_ranges=[(date(2025, 5, 1), date(2026, 4, 30))],
                today=TODAY,
            )
            == "gapped"
        )


class TestIsTrailingOnly:
    # Anchor on a fixed date so the trading-day arithmetic is stable.
    _ANCHOR = date(2024, 1, 22)  # Mon

    def test_trailing_when_single_range_within_7_trading_days(self) -> None:
        # Range starts 2024-01-12 (Fri) — 6 trading days back: 12,16,17,18,19,22 — within 7.
        assert is_trailing_only(
            missing_ranges=[(date(2024, 1, 12), date(2024, 1, 22))],
            today=self._ANCHOR,
            asset_class="equity",
        )

    def test_not_trailing_when_range_starts_8_or_more_trading_days_back(self) -> None:
        # Range starts 2024-01-02 (Tue) — 14 trading days back: outside window.
        assert not is_trailing_only(
            missing_ranges=[(date(2024, 1, 2), date(2024, 1, 22))],
            today=self._ANCHOR,
            asset_class="equity",
        )

    def test_multiple_ranges_never_count_as_trailing(self) -> None:
        assert not is_trailing_only(
            missing_ranges=[
                (date(2024, 1, 2), date(2024, 1, 5)),
                (date(2024, 1, 18), date(2024, 1, 22)),
            ],
            today=self._ANCHOR,
            asset_class="equity",
        )

    def test_empty_ranges_returns_false(self) -> None:
        assert not is_trailing_only(
            missing_ranges=[],
            today=self._ANCHOR,
            asset_class="equity",
        )
