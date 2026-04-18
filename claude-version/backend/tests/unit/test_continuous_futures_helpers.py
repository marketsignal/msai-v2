"""Unit tests for Databento continuous-futures symbology helpers."""

from __future__ import annotations

import pytest

from msai.services.nautilus.security_master.continuous_futures import (
    is_databento_continuous_pattern,
    raw_symbol_from_request,
)


@pytest.mark.parametrize("pattern", ["ES.Z.5", "NQ.Z.0", "RTY.c.2", "6E.H.1"])
def test_continuous_matches_valid(pattern: str) -> None:
    assert is_databento_continuous_pattern(pattern) is True


@pytest.mark.parametrize("pattern", ["ES", "AAPL.NASDAQ", "ESM6.CME", "ES.Z", "ES..5"])
def test_continuous_rejects_invalid(pattern: str) -> None:
    assert is_databento_continuous_pattern(pattern) is False


def test_raw_symbol_preserves_continuous() -> None:
    assert raw_symbol_from_request("ES.Z.5") == "ES.Z.5"


def test_raw_symbol_strips_concrete_venue() -> None:
    assert raw_symbol_from_request("AAPL.NASDAQ") == "AAPL"


def test_raw_symbol_passes_bare() -> None:
    assert raw_symbol_from_request("AAPL") == "AAPL"


def test_raw_symbol_rejects_empty() -> None:
    with pytest.raises(ValueError):
        raw_symbol_from_request("")
