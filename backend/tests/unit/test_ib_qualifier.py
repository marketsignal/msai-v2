"""Unit tests for :func:`spec_to_ib_contract` and :class:`IBQualifier`
(Phase 2 task 2.3).

The pure conversion function is tested exhaustively per asset
class. The async :class:`IBQualifier` is tested with a stub
provider so we don't need a live IB connection.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from msai.services.nautilus.security_master.ib_qualifier import (
    IBQualifier,
    spec_to_ib_contract,
)
from msai.services.nautilus.security_master.specs import InstrumentSpec

# ---------------------------------------------------------------------------
# spec_to_ib_contract — pure conversion per asset class
# ---------------------------------------------------------------------------


class TestSpecToContractEquity:
    def test_aapl_nasdaq(self) -> None:
        spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
        contract = spec_to_ib_contract(spec)
        assert contract.secType == "STK"
        assert contract.symbol == "AAPL"
        assert contract.exchange == "NASDAQ"
        assert contract.currency == "USD"

    def test_non_usd_currency(self) -> None:
        spec = InstrumentSpec(asset_class="equity", symbol="VOD", venue="LSE", currency="GBP")
        contract = spec_to_ib_contract(spec)
        assert contract.currency == "GBP"


class TestSpecToContractIndex:
    def test_spx_strips_caret_prefix(self) -> None:
        """The Nautilus ``InstrumentId`` uses ``^SPX`` but the IB
        ``Contract.symbol`` is the bare ``SPX``. Strip it before
        sending to IB."""
        spec = InstrumentSpec(asset_class="index", symbol="^SPX", venue="CBOE")
        contract = spec_to_ib_contract(spec)
        assert contract.secType == "IND"
        assert contract.symbol == "SPX"
        assert contract.exchange == "CBOE"

    def test_bare_symbol_also_works(self) -> None:
        spec = InstrumentSpec(asset_class="index", symbol="SPX", venue="CBOE")
        contract = spec_to_ib_contract(spec)
        assert contract.symbol == "SPX"


class TestSpecToContractFuture:
    def test_fixed_month_future(self) -> None:
        spec = InstrumentSpec(
            asset_class="future",
            symbol="ES",
            venue="CME",
            expiry=date(2025, 6, 20),
        )
        contract = spec_to_ib_contract(spec)
        assert contract.secType == "FUT"
        assert contract.symbol == "ES"
        assert contract.exchange == "CME"
        # IB resolves the actual last-trade date from yyyyMM — this
        # avoids the computed-3rd-Friday trap when a holiday shifts it.
        assert contract.lastTradeDateOrContractMonth == "202506"

    def test_fixed_month_future_juneteenth_holiday_uses_month_not_day(self) -> None:
        """Regression for multi-symbol drill 2026-04-20: when the 3rd
        Friday (2026-06-19) is Juneteenth, ESM6 actually settles
        2026-06-18. Passing yyyyMMdd='20260619' to IB fails; passing
        yyyyMM='202606' lets IB resolve the holiday-adjusted date."""
        spec = InstrumentSpec(
            asset_class="future",
            symbol="ES",
            venue="CME",
            expiry=date(2026, 6, 19),  # computed 3rd Friday (Juneteenth)
        )
        contract = spec_to_ib_contract(spec)
        assert contract.lastTradeDateOrContractMonth == "202606"

    def test_continuous_future(self) -> None:
        """``expiry=None`` → CONTFUT secType with no expiry field."""
        spec = InstrumentSpec(asset_class="future", symbol="ES", venue="CME")
        contract = spec_to_ib_contract(spec)
        assert contract.secType == "CONTFUT"
        assert contract.symbol == "ES"
        assert contract.lastTradeDateOrContractMonth == ""


class TestSpecToContractOption:
    def test_call_option(self) -> None:
        spec = InstrumentSpec(
            asset_class="option",
            symbol="AAPL",
            venue="SMART",
            expiry=date(2026, 5, 15),
            strike=Decimal("150.0"),
            right="C",
            underlying="AAPL",
        )
        contract = spec_to_ib_contract(spec)
        assert contract.secType == "OPT"
        assert contract.symbol == "AAPL"  # underlying
        assert contract.exchange == "SMART"
        assert contract.lastTradeDateOrContractMonth == "20260515"
        assert contract.strike == 150.0
        assert contract.right == "C"

    def test_put_option_different_underlying(self) -> None:
        """SPXW weekly on SPX: ``symbol`` stays as the underlying the
        IB contract keys on (SPX), not the derivative ticker."""
        spec = InstrumentSpec(
            asset_class="option",
            symbol="SPXW",
            venue="CBOE",
            expiry=date(2026, 3, 15),
            strike=Decimal("4500"),
            right="P",
            underlying="SPX",
        )
        contract = spec_to_ib_contract(spec)
        assert contract.symbol == "SPX"
        assert contract.right == "P"
        assert contract.strike == 4500.0


class TestSpecToContractForex:
    def test_eurusd(self) -> None:
        spec = InstrumentSpec(
            asset_class="forex",
            symbol="EUR",
            venue="IDEALPRO",
            currency="USD",
        )
        contract = spec_to_ib_contract(spec)
        assert contract.secType == "CASH"
        assert contract.symbol == "EUR"
        assert contract.exchange == "IDEALPRO"
        assert contract.currency == "USD"


# ---------------------------------------------------------------------------
# IBQualifier async adapter
# ---------------------------------------------------------------------------


class _StubProvider:
    """Minimal stub of Nautilus's
    :class:`InteractiveBrokersInstrumentProvider` — records the
    contracts it was asked to qualify and returns a canned
    Nautilus ``Instrument`` sentinel per spec.
    """

    def __init__(self, instrument_factory: Any) -> None:
        self.calls: list[Any] = []
        self._factory = instrument_factory

    async def get_instrument(self, contract: Any) -> Any:
        self.calls.append(contract)
        return self._factory(contract)


class TestIBQualifierQualify:
    @pytest.mark.asyncio
    async def test_qualify_single_spec_delegates_to_provider(self) -> None:
        """Happy path: spec → contract → provider.get_instrument →
        returned Instrument. The qualifier adds zero logic on top."""
        sentinel = object()
        provider = _StubProvider(lambda _: sentinel)
        qualifier = IBQualifier(provider=provider)  # type: ignore[arg-type]

        spec = InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ")
        result = await qualifier.qualify(spec)

        assert result is sentinel
        assert len(provider.calls) == 1
        assert provider.calls[0].secType == "STK"
        assert provider.calls[0].symbol == "AAPL"

    @pytest.mark.asyncio
    async def test_qualify_raises_when_provider_returns_none(self) -> None:
        """IB returned no match → the qualifier converts the silent
        ``None`` into a loud :class:`IBContractNotFoundError` so the
        caller doesn't accidentally treat an unresolved spec as resolved.
        Discriminates "no such contract" from generic ``ValueError``
        programmer-input errors via the typed exception class."""
        from msai.services.nautilus.security_master.ib_qualifier import (
            IBContractNotFoundError,
        )

        provider = _StubProvider(lambda _: None)
        qualifier = IBQualifier(provider=provider)  # type: ignore[arg-type]

        spec = InstrumentSpec(asset_class="equity", symbol="ZZZZ", venue="NASDAQ")
        with pytest.raises(IBContractNotFoundError, match="returned None"):
            await qualifier.qualify(spec)


class TestIBQualifierQualifyMany:
    @pytest.mark.asyncio
    async def test_qualify_many_is_sequential(self) -> None:
        """``qualify_many`` iterates sequentially (not via
        ``asyncio.gather``) so the provider's IB-pacing limits apply
        cleanly. We verify order is preserved."""
        # Generate distinct sentinel objects per call so we can
        # assert on order.
        instruments = [object() for _ in range(3)]
        it = iter(instruments)
        provider = _StubProvider(lambda _: next(it))
        qualifier = IBQualifier(provider=provider)  # type: ignore[arg-type]

        specs = [
            InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ"),
            InstrumentSpec(asset_class="equity", symbol="MSFT", venue="NASDAQ"),
            InstrumentSpec(asset_class="equity", symbol="GOOG", venue="NASDAQ"),
        ]
        results = await qualifier.qualify_many(specs)

        assert results == instruments
        assert [c.symbol for c in provider.calls] == ["AAPL", "MSFT", "GOOG"]

    @pytest.mark.asyncio
    async def test_qualify_many_propagates_mid_batch_failure(self) -> None:
        """A failure midway through the batch halts iteration —
        we don't silently swallow per-spec errors."""
        from msai.services.nautilus.security_master.ib_qualifier import (
            IBContractNotFoundError,
        )

        call_count = 0

        def _factory(_contract: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return None  # triggers IBContractNotFoundError inside qualify()
            return object()

        provider = _StubProvider(_factory)
        qualifier = IBQualifier(provider=provider)  # type: ignore[arg-type]

        specs = [
            InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ"),
            InstrumentSpec(asset_class="equity", symbol="BAD", venue="NASDAQ"),
            InstrumentSpec(asset_class="equity", symbol="MSFT", venue="NASDAQ"),
        ]
        with pytest.raises(IBContractNotFoundError, match="returned None"):
            await qualifier.qualify_many(specs)

        # We did call the provider for AAPL + BAD, but NOT MSFT —
        # the mid-batch raise halted iteration.
        assert call_count == 2


class TestIBQualifierQualifyContract:
    """``qualify_contract`` accepts a pre-built ``IBContract`` directly,
    bypassing the ``InstrumentSpec`` → contract conversion. Used by the
    CLI's per-asset-class factories where the contract shape is already
    known."""

    @pytest.mark.asyncio
    async def test_qualify_contract_delegates_to_provider(self) -> None:
        """Happy path: contract handed verbatim to provider.get_instrument;
        returned Instrument propagates back."""
        from nautilus_trader.adapters.interactive_brokers.common import IBContract

        sentinel = object()
        provider = _StubProvider(lambda _: sentinel)
        qualifier = IBQualifier(provider=provider)  # type: ignore[arg-type]

        contract = IBContract(
            secType="STK",
            symbol="AAPL",
            exchange="SMART",
            primaryExchange="NASDAQ",
            currency="USD",
        )
        result = await qualifier.qualify_contract(contract)

        assert result is sentinel
        assert len(provider.calls) == 1
        # Same contract instance handed to the provider — no spec-shaped
        # transformation in between.
        assert provider.calls[0] is contract

    @pytest.mark.asyncio
    async def test_qualify_contract_raises_on_provider_miss(self) -> None:
        """Provider miss surfaces as a loud :class:`IBContractNotFoundError`
        mentioning the contract — same shape as :meth:`qualify`'s spec-miss
        raise. Inherits :class:`LookupError` so callers can discriminate
        "no such contract" from generic :class:`ValueError`."""
        from nautilus_trader.adapters.interactive_brokers.common import IBContract

        from msai.services.nautilus.security_master.ib_qualifier import (
            IBContractNotFoundError,
        )

        provider = _StubProvider(lambda _: None)
        qualifier = IBQualifier(provider=provider)  # type: ignore[arg-type]

        contract = IBContract(secType="STK", symbol="BOGUS", exchange="SMART", currency="USD")
        with pytest.raises(IBContractNotFoundError, match="returned None for contract"):
            await qualifier.qualify_contract(contract)


# ---------------------------------------------------------------------------
# IBQualifier.listing_venue_for — extracts IB primaryExchange with fallback
# ---------------------------------------------------------------------------


class _ListingVenueProvider:
    """Minimal stub that exposes ``contract_details`` keyed by ``InstrumentId``.

    Mirrors the slice of :class:`InteractiveBrokersInstrumentProvider`'s public
    surface that :meth:`IBQualifier.listing_venue_for` consults — enough to
    exercise the four branches (no-provider / no-details / no-contract /
    no-primary / primary-present) without importing the heavy real provider.
    """

    def __init__(self, details_map: dict[Any, Any]) -> None:
        self.contract_details: dict[Any, Any] = details_map


class _ContractDetailsStub:
    def __init__(self, contract: Any) -> None:
        self.contract = contract


class _ContractStub:
    def __init__(self, primary_exchange: Any) -> None:
        self.primaryExchange = primary_exchange  # noqa: N815 — IB SDK field name


class TestListingVenueFor:
    """Coverage for the four ``listing_venue_for`` branches."""

    def _equity_instrument(self) -> Any:
        from nautilus_trader.test_kit.providers import (  # noqa: PLC0415
            TestInstrumentProvider,
        )

        return TestInstrumentProvider.equity(symbol="AAPL", venue="NASDAQ")

    def test_returns_primary_exchange_when_present(self) -> None:
        """Happy path: ``contract.primaryExchange`` populated → returned."""
        instrument = self._equity_instrument()
        details_map = {
            instrument.id: _ContractDetailsStub(_ContractStub(primary_exchange="NASDAQ"))
        }
        qualifier = IBQualifier(provider=_ListingVenueProvider(details_map))  # type: ignore[arg-type]

        assert qualifier.listing_venue_for(instrument) == "NASDAQ"

    def test_falls_back_to_routing_venue_when_primary_empty(self) -> None:
        """``primaryExchange`` empty/None → routing venue (instrument's own)."""
        instrument = self._equity_instrument()
        details_map = {instrument.id: _ContractDetailsStub(_ContractStub(primary_exchange=""))}
        qualifier = IBQualifier(provider=_ListingVenueProvider(details_map))  # type: ignore[arg-type]

        assert qualifier.listing_venue_for(instrument) == "NASDAQ"

    def test_falls_back_when_provider_has_no_contract_details_for_instrument(self) -> None:
        """Provider's ``contract_details`` lacks an entry for this instrument
        (e.g. forex via IDEALPRO) → return routing venue verbatim."""
        instrument = self._equity_instrument()
        qualifier = IBQualifier(provider=_ListingVenueProvider({}))  # type: ignore[arg-type]

        assert qualifier.listing_venue_for(instrument) == "NASDAQ"

    def test_falls_back_when_provider_is_none(self) -> None:
        """No provider attached at all → routing venue verbatim."""
        instrument = self._equity_instrument()
        qualifier = IBQualifier(provider=None)  # type: ignore[arg-type]

        assert qualifier.listing_venue_for(instrument) == "NASDAQ"
