import pytest

from msai.services.nautilus.security_master.venue_normalization import (
    UnknownDatabentoVenueError,
    normalize_alias_for_registry,
    normalize_databento_alias_for_lookup,
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


# ──────────────────────────────────────────────────────────────────────
# normalize_databento_alias_for_lookup — read-boundary helper.
# Accepts both Databento MIC and exchange-name forms; idempotent on the
# latter; fail-loud on unknown suffixes. See revised Codex Item 4
# (2026-05-12 fresh-VM-data-path-closure PR).
# ──────────────────────────────────────────────────────────────────────


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
def test_lookup_mic_translates_to_exchange_name(alias, expected):
    assert normalize_databento_alias_for_lookup(alias) == expected


@pytest.mark.parametrize(
    "canonical_alias",
    [
        "AAPL.NASDAQ",
        "SPY.ARCA",
        "BRK.B.NYSE",
        "MEMX.MEMX",
        "ESM6.CME",
    ],
)
def test_lookup_exchange_name_input_is_idempotent(canonical_alias):
    """Already-canonical exchange-name input returns unchanged."""
    assert normalize_databento_alias_for_lookup(canonical_alias) == canonical_alias


def test_lookup_unknown_suffix_raises_loud():
    """Anything that isn't a known MIC or exchange-name must fail."""
    with pytest.raises(UnknownDatabentoVenueError) as exc_info:
        normalize_databento_alias_for_lookup("AAPL.FAKEMIC")
    assert "FAKEMIC" in str(exc_info.value)
    # Surface BOTH valid input shapes in the error so the operator knows
    # which forms are accepted.
    assert "MIC" in str(exc_info.value)
    assert "exchange name" in str(exc_info.value)


def test_lookup_no_venue_suffix_raises():
    """A bare ticker must hit the bare-ticker path, not this helper."""
    with pytest.raises(UnknownDatabentoVenueError):
        normalize_databento_alias_for_lookup("AAPL")


def test_write_boundary_still_rejects_exchange_name_input():
    """Regression guard: the write-boundary helper intentionally rejects
    already-translated input so an upstream caller can't double-normalize
    and silently store a row whose ``source_venue_raw`` lineage is wrong.
    The contract on writing is unchanged by adding the read helper.
    """
    with pytest.raises(UnknownDatabentoVenueError):
        normalize_alias_for_registry("databento", "AAPL.NASDAQ")
