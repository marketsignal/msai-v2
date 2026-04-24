import pytest

from msai.services.nautilus.security_master.venue_normalization import (
    UnknownDatabentoVenueError,
    normalize_alias_for_registry,
)


@pytest.mark.parametrize(
    "alias, expected",
    [
        ("AAPL.XNAS", "AAPL.NASDAQ"),
        ("SPY.XARC", "SPY.ARCA"),
        ("IWM.ARCX", "IWM.ARCA"),
        ("BRK.B.XNYS", "BRK.B.NYSE"),
        ("PEARL.EPRL", "PEARL.PEARL"),
        ("ESM6.GLBX", "ESM6.CME"),
    ],
)
def test_known_mic_normalizes(alias, expected):
    assert normalize_alias_for_registry("databento", alias) == expected


def test_ib_alias_passthrough():
    assert normalize_alias_for_registry("interactive_brokers", "AAPL.NASDAQ") == "AAPL.NASDAQ"


def test_unknown_mic_raises_loud():
    with pytest.raises(UnknownDatabentoVenueError) as exc_info:
        normalize_alias_for_registry("databento", "AAPL.FAKEMIC")
    assert "FAKEMIC" in str(exc_info.value)
    assert "AAPL.FAKEMIC" in str(exc_info.value)


def test_no_venue_suffix_raises():
    with pytest.raises(UnknownDatabentoVenueError):
        normalize_alias_for_registry("databento", "AAPL")
