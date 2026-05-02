"""Unit tests for inventory status derivation and trailing-only detection."""

from __future__ import annotations

from datetime import date

from msai.services.symbol_onboarding.inventory import (
    derive_status,
    is_trailing_only,
)

TODAY = date(2026, 5, 1)


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

    def test_stale_when_only_trailing_month_missing(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=True,
                coverage_status="gapped",
                missing_ranges=[(date(2026, 4, 1), date(2026, 4, 30))],
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
                    (date(2026, 4, 1), date(2026, 4, 30)),
                ],
                today=TODAY,
            )
            == "gapped"
        )

    def test_priority_order_data_beats_registration(self) -> None:
        assert (
            derive_status(
                registered=True,
                bt_avail=True,
                live=False,
                coverage_status="gapped",
                missing_ranges=[(date(2026, 4, 1), date(2026, 4, 30))],
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
    def test_empty_is_not_trailing(self) -> None:
        assert is_trailing_only(missing_ranges=[], today=TODAY) is False

    def test_single_trailing_month_is_trailing(self) -> None:
        assert (
            is_trailing_only(
                missing_ranges=[(date(2026, 4, 1), date(2026, 4, 30))],
                today=TODAY,
            )
            is True
        )

    def test_old_missing_alone_is_not_trailing(self) -> None:
        assert (
            is_trailing_only(
                missing_ranges=[(date(2024, 3, 1), date(2024, 3, 31))],
                today=TODAY,
            )
            is False
        )

    def test_trailing_plus_old_is_not_trailing_only(self) -> None:
        assert (
            is_trailing_only(
                missing_ranges=[
                    (date(2024, 3, 1), date(2024, 3, 31)),
                    (date(2026, 4, 1), date(2026, 4, 30)),
                ],
                today=TODAY,
            )
            is False
        )

    def test_single_trailing_range_spanning_two_months_is_not_trailing(self) -> None:
        # Per tightened rule (iter-1 fix): start must be >= prev_month_start.
        # Range start 2026-03-01 < prev_month_start 2026-04-01 → False.
        assert (
            is_trailing_only(
                missing_ranges=[(date(2026, 3, 1), date(2026, 4, 30))],
                today=TODAY,
            )
            is False
        )

    def test_long_multi_month_gap_is_not_trailing(self) -> None:
        assert (
            is_trailing_only(
                missing_ranges=[(date(2025, 5, 1), date(2026, 4, 30))],
                today=TODAY,
            )
            is False
        )

    def test_two_separate_ranges_both_trailing_is_not_trailing_only(self) -> None:
        assert (
            is_trailing_only(
                missing_ranges=[
                    (date(2026, 3, 1), date(2026, 3, 31)),
                    (date(2026, 4, 15), date(2026, 4, 30)),
                ],
                today=TODAY,
            )
            is False
        )
