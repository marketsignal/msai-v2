"""Tests for the backtest error-envelope + remediation Pydantic models."""

from __future__ import annotations

from datetime import date
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from msai.schemas.backtest import (
    BacktestResultsResponse,
    ErrorEnvelope,
    Remediation,
    SeriesDailyPoint,
    SeriesMonthlyReturn,
    SeriesPayload,
    SeriesStatus,
)


class TestRemediation:
    def test_ingest_data_kind_happy_path(self):
        r = Remediation(
            kind="ingest_data",
            symbols=["ES.n.0"],
            asset_class="futures",
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 15),
        )
        assert r.auto_available is False  # MVP default

    def test_kind_is_literal_union(self):
        # Unknown kinds are rejected — forward-compat via Literal expansion, not duck typing.
        with pytest.raises(ValueError):
            Remediation(kind="nuke_the_server")  # type: ignore[arg-type]

    def test_none_kind_is_valid_placeholder(self):
        r = Remediation(kind="none")
        assert r.symbols is None
        assert r.auto_available is False


class TestErrorEnvelope:
    def test_minimal_envelope(self):
        e = ErrorEnvelope(code="unknown", message="something broke")
        assert e.suggested_action is None
        assert e.remediation is None

    def test_full_envelope_round_trips_through_json(self):
        e = ErrorEnvelope(
            code="missing_data",
            message="<DATA_ROOT>/parquet/stocks/ES is empty",
            suggested_action="Run: msai ingest stocks ES 2025-01-02 2025-01-15",
            remediation=Remediation(
                kind="ingest_data",
                symbols=["ES"],
                asset_class="stocks",
                start_date=date(2025, 1, 2),
                end_date=date(2025, 1, 15),
            ),
        )
        dumped = e.model_dump(mode="json")
        assert dumped["remediation"]["start_date"] == "2025-01-02"
        assert dumped["remediation"]["auto_available"] is False
        reparsed = ErrorEnvelope.model_validate(dumped)
        assert reparsed == e


# ---------------------------------------------------------------------------
# Canonical analytics series payload
# ---------------------------------------------------------------------------


def test_series_status_enum_values() -> None:
    assert set(get_args(SeriesStatus)) == {"ready", "not_materialized", "failed"}


def test_series_daily_point_validation() -> None:
    p = SeriesDailyPoint(
        date="2024-01-02",
        equity=100250.50,
        drawdown=-0.05,
        daily_return=0.0025,
    )
    assert p.date == "2024-01-02"
    assert p.drawdown <= 0  # invariant: drawdown is non-positive

    with pytest.raises(ValidationError):
        SeriesDailyPoint(date="not-a-date", equity=100_000, drawdown=0, daily_return=0)

    # Negative equity is a data bug — no short-base-value convention in
    # this codebase. Zero is legitimate (total-loss day) and is tested
    # separately in :func:`test_series_daily_point_accepts_zero_equity`.
    with pytest.raises(ValidationError):
        SeriesDailyPoint(date="2024-01-02", equity=-100.0, drawdown=-0.01, daily_return=0.0)

    # drawdown must be non-positive — a positive value violates the invariant
    with pytest.raises(ValidationError):
        SeriesDailyPoint(date="2024-01-02", equity=100_000.0, drawdown=0.05, daily_return=0.0)


def test_series_monthly_return_format() -> None:
    m = SeriesMonthlyReturn(month="2024-01", pct=0.0512)
    assert m.month == "2024-01"

    with pytest.raises(ValidationError):
        SeriesMonthlyReturn(month="2024-1", pct=0.05)  # must be zero-padded

    # Regex + validator reject out-of-range / garbage months that the old
    # ``^\d{4}-\d{2}$`` pattern silently accepted.
    with pytest.raises(ValidationError):
        SeriesMonthlyReturn(month="2024-13", pct=0.05)  # month > 12
    with pytest.raises(ValidationError):
        SeriesMonthlyReturn(month="2024-00", pct=0.05)  # month == 0
    with pytest.raises(ValidationError):
        SeriesMonthlyReturn(month="2024-99", pct=0.05)  # garbage


def test_series_payload_round_trip() -> None:
    payload = SeriesPayload(
        daily=[
            SeriesDailyPoint(
                date="2024-01-02",
                equity=100_000.0,
                drawdown=0.0,
                daily_return=0.0,
            )
        ],
        monthly_returns=[SeriesMonthlyReturn(month="2024-01", pct=0.05)],
    )
    dumped = payload.model_dump(mode="json")
    restored = SeriesPayload.model_validate(dumped)
    assert restored == payload


def test_series_payload_accepts_empty_lists() -> None:
    """Empty payloads are valid — legacy backtests and zero-trade runs need this path."""
    empty = SeriesPayload(daily=[], monthly_returns=[])
    assert empty.daily == []
    assert empty.monthly_returns == []
    # Round-trip
    restored = SeriesPayload.model_validate(empty.model_dump(mode="json"))
    assert restored == empty


# ---------------------------------------------------------------------------
# Extended BacktestResultsResponse (series + series_status + has_report,
# inline trades field removed in favor of paginated /trades endpoint shipped in B8).
# ---------------------------------------------------------------------------


def test_backtest_results_response_has_series_and_status() -> None:
    fields = BacktestResultsResponse.model_fields
    assert "series" in fields
    assert "series_status" in fields
    assert "has_report" in fields
    # trades removed — paginated /trades endpoint replaces inline delivery
    assert "trades" not in fields


def test_backtest_results_response_round_trip_with_series() -> None:
    series = SeriesPayload(
        daily=[
            SeriesDailyPoint(
                date="2024-01-02",
                equity=100_500.0,
                drawdown=0.0,
                daily_return=0.005,
            )
        ],
        monthly_returns=[SeriesMonthlyReturn(month="2024-01", pct=0.005)],
    )
    response = BacktestResultsResponse(
        id=uuid4(),
        metrics={"sharpe_ratio": 1.2},
        trade_count=10,
        series=series,
        series_status="ready",
        has_report=True,
    )
    dumped = response.model_dump(mode="json")
    restored = BacktestResultsResponse.model_validate(dumped)
    assert restored == response


def test_backtest_results_response_accepts_not_materialized() -> None:
    resp = BacktestResultsResponse(
        id=uuid4(),
        metrics=None,
        trade_count=0,
        series=None,
        series_status="not_materialized",
        has_report=False,
    )
    assert resp.series is None
    assert resp.has_report is False


def test_backtest_results_response_defaults() -> None:
    """Fields have correct defaults when unset — legacy rows + legacy clients."""
    resp = BacktestResultsResponse(id=uuid4(), trade_count=0)
    assert resp.series is None
    assert resp.series_status == "not_materialized"
    assert resp.has_report is False


def test_results_response_rejects_ready_without_series() -> None:
    """The model_validator must reject ``series_status="ready"`` with
    ``series=None`` — that's a half-written worker transaction (status
    flipped but payload not persisted) and a silent acceptance would let
    the UI render a "ready but empty" card with no error signal.
    """
    with pytest.raises(ValidationError, match="series payload"):
        BacktestResultsResponse(
            id=uuid4(),
            trade_count=0,
            series=None,
            series_status="ready",
        )


def test_results_response_rejects_non_ready_with_series() -> None:
    """Mirror: ``series_status != "ready"`` must not carry a payload —
    guards against a manual SQL repair / migration slip that leaves a
    stale payload behind when the status was rewritten.
    """
    valid_series = {
        "daily": [
            {
                "date": "2024-01-02",
                "equity": 100.0,
                "drawdown": 0.0,
                "daily_return": 0.0,
            }
        ],
        "monthly_returns": [],
    }
    with pytest.raises(ValidationError, match="must not carry a series"):
        BacktestResultsResponse(
            id=uuid4(),
            trade_count=0,
            series=valid_series,
            series_status="failed",
        )


def test_series_daily_point_accepts_zero_equity() -> None:
    """Total-loss day: ``daily_return == -1.0`` yields ``equity == 0.0``
    via the cumprod chain. The schema must accept this so a blowup run
    still populates charts.
    """
    p = SeriesDailyPoint(date="2024-01-02", equity=0.0, drawdown=-1.0, daily_return=-1.0)
    assert p.equity == 0.0


def test_series_daily_point_rejects_negative_equity() -> None:
    """Negative equity is a data bug (no short-base-value convention) —
    the schema must still reject it even after the ``gt → ge`` relaxation
    that admitted zero.
    """
    with pytest.raises(ValidationError):
        SeriesDailyPoint(date="2024-01-02", equity=-1.0, drawdown=-2.0, daily_return=-2.0)
