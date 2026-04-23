"""Unit tests for msai.services.analytics_math.normalize_daily_returns.

The canonical returns normalizer feeds BOTH the QuantStats report generator
and the persisted ``Backtest.series`` payload. These tests pin the contract.
"""

from __future__ import annotations

import pandas as pd
import pytest

from msai.services.analytics_math import normalize_daily_returns


def test_normalize_daily_returns_compounds_intraday_to_daily() -> None:
    # 3 intraday bars on 2024-01-02, 2 bars on 2024-01-03 (all tz-aware UTC)
    idx = pd.DatetimeIndex(
        [
            "2024-01-02 09:30",
            "2024-01-02 12:00",
            "2024-01-02 15:59",
            "2024-01-03 09:30",
            "2024-01-03 15:59",
        ],
        tz="UTC",
    )
    returns = pd.Series([0.01, -0.005, 0.003, 0.008, -0.002], index=idx, name="returns")
    daily = normalize_daily_returns(returns)
    assert len(daily) == 2
    assert daily.index[0].strftime("%Y-%m-%d") == "2024-01-02"
    # (1.01 * 0.995 * 1.003) - 1
    assert daily.iloc[0] == pytest.approx((1.01 * 0.995 * 1.003) - 1, rel=1e-6)


def test_normalize_daily_returns_handles_tz_naive() -> None:
    idx = pd.date_range("2024-01-02", periods=3, freq="D")  # tz-naive
    returns = pd.Series([0.01, 0.02, -0.01], index=idx, name="returns")
    result = normalize_daily_returns(returns)
    # Pre-move behavior: tz-naive DatetimeIndex passes through as-is (no re-parse);
    # downstream groupby-day still runs, so day-count is preserved.
    assert len(result) == 3


def test_normalize_daily_returns_empty_input() -> None:
    empty = pd.Series(dtype=float, name="returns")
    result = normalize_daily_returns(empty)
    assert len(result) == 0


def test_normalize_daily_returns_accepts_none() -> None:
    """None input returns an empty Series (legacy contract)."""
    result = normalize_daily_returns(None)
    assert result.empty


def test_normalize_daily_returns_preserves_zero_return_days() -> None:
    """Legitimate zero-return days must be retained (unlike no-data days)."""
    idx = pd.date_range("2024-01-02", periods=3, freq="D", tz="UTC")
    returns = pd.Series([0.0, 0.01, 0.0], index=idx)
    result = normalize_daily_returns(returns)
    assert len(result) == 3
    assert result.iloc[0] == pytest.approx(0.0)
    assert result.iloc[2] == pytest.approx(0.0)


def test_normalize_daily_returns_coerces_string_dates() -> None:
    """String-parsed date index is coerced to UTC datetime (legacy data path)."""
    returns = pd.Series(
        [0.01, 0.02],
        index=pd.Index(["2024-01-02", "2024-01-03"], dtype=object),
    )
    result = normalize_daily_returns(returns)
    assert isinstance(result.index, pd.DatetimeIndex)
    assert result.index.tz is not None
    assert len(result) == 2


class TestBuildSeriesPayload:
    """Tests for :func:`build_series_payload`.

    Pins the canonical ``SeriesPayload`` dict contract emitted by the worker
    and consumed by the ``/results`` endpoint + frontend detail page.
    """

    def test_builds_daily_and_monthly_from_returns(self) -> None:
        from msai.services.analytics_math import build_series_payload

        idx = pd.date_range("2024-01-02", periods=5, freq="B", tz="UTC")
        returns = pd.Series([0.01, -0.005, 0.003, 0.008, -0.002], index=idx)

        payload = build_series_payload(returns)

        assert len(payload["daily"]) == 5
        assert payload["daily"][0]["date"] == "2024-01-02"
        assert payload["daily"][0]["equity"] == pytest.approx(101_000.0, rel=1e-6)
        assert payload["daily"][0]["drawdown"] == 0.0  # first day, new high → no drawdown
        assert len(payload["monthly_returns"]) == 1
        assert payload["monthly_returns"][0]["month"] == "2024-01"

    def test_drawdown_is_non_positive(self) -> None:
        from msai.services.analytics_math import build_series_payload

        idx = pd.date_range("2024-01-02", periods=3, freq="D", tz="UTC")
        returns = pd.Series([0.02, -0.03, 0.01], index=idx)
        payload = build_series_payload(returns)
        drawdowns = [p["drawdown"] for p in payload["daily"]]
        assert all(d <= 0.0 for d in drawdowns)

    def test_multi_month_produces_multi_monthly(self) -> None:
        from msai.services.analytics_math import build_series_payload

        # 40 business days starting 2024-01-02 lands on 2024-02-26 (22 biz
        # days in Jan + 18 into Feb), yielding exactly two month-end buckets.
        idx = pd.date_range("2024-01-02", periods=40, freq="B", tz="UTC")
        returns = pd.Series([0.001] * 40, index=idx)
        payload = build_series_payload(returns)
        assert len(payload["monthly_returns"]) == 2  # Jan + Feb
        assert payload["monthly_returns"][0]["month"] == "2024-01"
        assert payload["monthly_returns"][1]["month"] == "2024-02"

    def test_empty_returns_yields_empty_payload(self) -> None:
        from msai.services.analytics_math import build_series_payload

        payload = build_series_payload(pd.Series(dtype=float))
        assert payload == {"daily": [], "monthly_returns": []}

    def test_payload_validates_against_pydantic(self) -> None:
        """Output must round-trip through SeriesPayload validation."""
        from msai.schemas.backtest import SeriesPayload
        from msai.services.analytics_math import build_series_payload

        idx = pd.date_range("2024-01-02", periods=5, freq="B", tz="UTC")
        returns = pd.Series([0.01, -0.005, 0.003, 0.008, -0.002], index=idx)
        payload_dict = build_series_payload(returns)
        SeriesPayload.model_validate(payload_dict)  # raises if invalid
