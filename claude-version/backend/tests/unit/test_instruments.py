"""Unit tests for :mod:`msai.services.nautilus.instruments`
(Phase 2 tasks 2.6 + 2.10)."""

from __future__ import annotations

from msai.services.nautilus.instruments import (
    DEFAULT_EQUITY_VENUE,
    canonical_instrument_id,
    default_bar_type,
    resolve_instrument,
)


class TestResolveInstrument:
    def test_bare_symbol_defaults_to_nasdaq(self) -> None:
        """The default venue is ``NASDAQ`` — matches what
        ``SecurityMaster`` would return from its cache for a bare
        ``"AAPL"`` request. This is the primary Phase 2 contract:
        no more ``*.SIM`` rebinding for equity lookups."""
        inst = resolve_instrument("AAPL")
        assert str(inst.id) == "AAPL.NASDAQ"
        assert inst.raw_symbol.value == "AAPL"

    def test_explicit_venue_kwarg(self) -> None:
        inst = resolve_instrument("MSFT", venue="NYSE")
        assert str(inst.id) == "MSFT.NYSE"

    def test_dotted_id_suffix_wins_over_venue_kwarg(self) -> None:
        """If a fully-qualified ID is passed, respect its suffix
        regardless of the ``venue`` kwarg — the SecurityMaster
        round-trip contract keys on the canonical ID string and
        must not silently rewrite venues."""
        inst = resolve_instrument("ESM5.XCME", venue="NASDAQ")
        assert str(inst.id) == "ESM5.XCME"

    def test_default_equity_venue_is_nasdaq(self) -> None:
        assert DEFAULT_EQUITY_VENUE == "NASDAQ"


class TestCanonicalInstrumentId:
    def test_bare_symbol(self) -> None:
        assert canonical_instrument_id("AAPL") == "AAPL.NASDAQ"

    def test_explicit_venue(self) -> None:
        assert canonical_instrument_id("VOD", venue="LSE") == "VOD.LSE"


class TestDefaultBarType:
    def test_bar_type_string_format(self) -> None:
        assert default_bar_type("AAPL") == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"


class TestLegacyResolveSimRemoved:
    """Task 2.10: ``legacy_resolve_sim`` was deleted. A regression
    guard so any future commit reintroducing the shim fails CI."""

    def test_legacy_shim_no_longer_importable(self) -> None:
        import msai.services.nautilus.instruments as instruments_module

        assert not hasattr(instruments_module, "legacy_resolve_sim"), (
            "legacy_resolve_sim was removed in Task 2.10. Migrate "
            "callers to resolve_instrument(symbol, venue=...)."
        )


class TestStructuralIdentityVsSecurityMaster:
    def test_bare_symbol_instrument_has_nautilus_equity_type(self) -> None:
        """The returned object is a real Nautilus ``Equity``
        (subclass of ``Instrument``), so a SecurityMaster cache
        round-trip that writes ``instrument.to_dict`` and reads it
        back via ``Equity.from_dict`` works — the two paths
        produce structurally identical objects."""
        from nautilus_trader.model.instruments import Equity

        inst = resolve_instrument("AAPL")
        assert isinstance(inst, Equity)
