"""Unit tests for :mod:`msai.services.nautilus.instruments`."""

from __future__ import annotations

from msai.services.nautilus.instruments import (
    DEFAULT_EQUITY_VENUE,
    default_bar_type,
    resolve_instrument,
)


class TestResolveInstrument:
    def test_bare_symbol_defaults_to_nasdaq(self) -> None:
        """The default venue is ``NASDAQ`` — matches what
        ``SecurityMaster`` would return from its cache for a bare
        ``"AAPL"`` request. No more ``*.SIM`` rebinding for equity
        lookups."""
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
        inst = resolve_instrument("ESM5.CME", venue="NASDAQ")
        assert str(inst.id) == "ESM5.CME"

    def test_default_equity_venue_is_nasdaq(self) -> None:
        assert DEFAULT_EQUITY_VENUE == "NASDAQ"


class TestDefaultBarType:
    def test_default_bar_type_inlines_resolve_instrument(self) -> None:
        """default_bar_type returns canonical-id-shaped string without
        calling the deleted canonical_instrument_id helper."""
        assert default_bar_type("AAPL") == "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"
        assert default_bar_type("VOD", venue="LSE") == "VOD.LSE-1-MINUTE-LAST-EXTERNAL"


class TestCanonicalInstrumentIdRemoved:
    """Regression guard: ``canonical_instrument_id`` is not exported
    from this module any more. Any future commit reintroducing the
    helper fails CI."""

    def test_canonical_instrument_id_is_not_importable(self) -> None:
        import msai.services.nautilus.instruments as mod

        assert not hasattr(mod, "canonical_instrument_id"), (
            "canonical_instrument_id must be deleted from instruments.py"
        )


class TestLegacyResolveSimRemoved:
    """``legacy_resolve_sim`` was deleted. Regression guard so any
    future commit reintroducing the shim fails CI."""

    def test_legacy_shim_no_longer_importable(self) -> None:
        import msai.services.nautilus.instruments as instruments_module

        assert not hasattr(instruments_module, "legacy_resolve_sim"), (
            "legacy_resolve_sim was removed. Migrate callers to "
            "resolve_instrument(symbol, venue=...)."
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
