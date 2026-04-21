"""Tests for the backtest error-envelope + remediation Pydantic models."""

from __future__ import annotations

from datetime import date

import pytest

from msai.schemas.backtest import ErrorEnvelope, Remediation


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
