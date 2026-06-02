"""Unit tests for the pure / sync surface of ``symbology_shim``.

The async outbound path (:func:`resolve_for_databento`) needs a real
Postgres for the registry seed; it's covered by the integration suite at
``tests/integration/services/test_symbology_shim_resolve.py``. This file
exercises:

* :func:`retag_inbound_bar` — Bar/BarType re-tag (pure transformation).
* :func:`resolve_for_databento` non-IBKR venue rejection (fails before
  any DB call, so no session is required).

T11's ``SymbologyShimActor`` consumes the audit-metadata sink signature
verified here.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.objects import Price, Quantity

from msai.services.symbology_shim import (
    DatabentoSubscriptionTarget,
    resolve_for_databento,
    retag_inbound_bar,
)


def _make_bar(*, venue: str = "XNAS", symbol: str = "AAPL", ts_event: int = 1_000_000_000) -> Bar:
    """Build a 1-minute synthetic ``Bar`` tagged ``<symbol>.<venue>``."""
    bar_type = BarType.from_str(f"{symbol}.{venue}-1-MINUTE-LAST-EXTERNAL")
    return Bar(
        bar_type,
        Price.from_str("100.0"),
        Price.from_str("101.0"),
        Price.from_str("99.0"),
        Price.from_str("100.5"),
        Quantity.from_str("1000"),
        ts_event,
        ts_event + 1,
    )


# --- retag_inbound_bar ---------------------------------------------------


def test_retag_inbound_bar_rewrites_xnas_venue_to_ibkr() -> None:
    # Arrange
    bar = _make_bar(venue="XNAS", symbol="AAPL")
    assert bar.bar_type.instrument_id.venue == Venue("XNAS")

    # Act
    retagged = retag_inbound_bar(bar)

    # Assert
    assert retagged.bar_type.instrument_id.venue == Venue("IBKR")
    assert retagged.bar_type.instrument_id.symbol == Symbol("AAPL")
    # OHLCV + timestamps preserved verbatim.
    assert retagged.open == bar.open
    assert retagged.high == bar.high
    assert retagged.low == bar.low
    assert retagged.close == bar.close
    assert retagged.volume == bar.volume
    assert retagged.ts_event == bar.ts_event
    assert retagged.ts_init == bar.ts_init
    # Spec + aggregation source preserved.
    assert retagged.bar_type.spec == bar.bar_type.spec
    assert retagged.bar_type.aggregation_source == bar.bar_type.aggregation_source


def test_retag_inbound_bar_invokes_audit_sink_with_provenance_dict() -> None:
    # Arrange
    bar = _make_bar(venue="XNAS", symbol="AAPL", ts_event=42)
    captured: list[dict[str, Any]] = []

    # Act
    retag_inbound_bar(bar, audit_metadata_sink=captured.append)

    # Assert — sink called exactly once with all required keys.
    assert len(captured) == 1
    meta = captured[0]
    assert meta["provider"] == "databento"
    assert meta["dataset"] == "EQUS.MINI"
    assert meta["native_venue"] == "XNAS"
    assert meta["native_symbol"] == "AAPL"
    assert meta["original_instrument_id"] == "AAPL.XNAS"
    assert meta["ts_event"] == 42


def test_retag_inbound_bar_with_ibkr_venue_returns_original_no_audit_call() -> None:
    """Idempotency fast-path: a bar that's already tagged IBKR is returned
    unchanged and the audit sink is NOT invoked."""
    # Arrange
    bar = _make_bar(venue="IBKR", symbol="AAPL")
    captured: list[dict[str, Any]] = []

    # Act
    result = retag_inbound_bar(bar, audit_metadata_sink=captured.append)

    # Assert
    assert result is bar  # exact-same object — no allocation on the fast path
    assert captured == []  # sink never called


def test_retag_inbound_bar_sink_is_optional() -> None:
    """Calling without an audit sink must not raise."""
    bar = _make_bar(venue="XNAS", symbol="SPY")
    retagged = retag_inbound_bar(bar)  # no kwarg
    assert retagged.bar_type.instrument_id.venue == Venue("IBKR")
    assert retagged.bar_type.instrument_id.symbol == Symbol("SPY")


def test_retag_inbound_bar_unknown_native_venue_emits_dataset_none() -> None:
    """A bar from a venue not in the equity-mapping table (e.g. a future
    GLBX entry) yields ``dataset=None`` in the audit dict instead of
    raising — the re-tag itself is still safe; the audit just flags the
    gap so downstream observers see it."""
    # Arrange
    bar = _make_bar(venue="GLBX", symbol="ES")
    captured: list[dict[str, Any]] = []

    # Act
    retagged = retag_inbound_bar(bar, audit_metadata_sink=captured.append)

    # Assert
    assert retagged.bar_type.instrument_id.venue == Venue("IBKR")
    assert len(captured) == 1
    assert captured[0]["dataset"] is None
    assert captured[0]["native_venue"] == "GLBX"


# --- resolve_for_databento — pre-DB validation --------------------------


@pytest.mark.asyncio
async def test_resolve_for_databento_rejects_non_ibkr_venue() -> None:
    """Pre-flight venue check fires before any DB call, so we can run this
    without a session fixture."""
    with pytest.raises(ValueError, match="expects canonical .IBKR venue"):
        await resolve_for_databento(
            InstrumentId.from_str("AAPL.XNAS"),
            session=None,  # type: ignore[arg-type]
            as_of_date=date(2026, 5, 29),
        )


# --- resolve_for_databento — asset-class pin / unpinned-first -----------


@pytest.mark.asyncio
async def test_resolve_for_databento_uses_unpinned_lookup_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex iter 12 P2 regression: a non-equity portfolio member (e.g.
    an ``ES`` futures strategy) must trip the ``NotImplementedError``
    guard so the supervisor can fall back to the legacy single-gateway
    builder. The shim therefore does an UNPINNED ``lookup_for_live``
    first so the resolver returns the actual asset class, and the
    asset-class guard below catches non-equity members.

    This test locks the contract: the FIRST ``lookup_for_live`` call
    MUST be unpinned (``asset_class=None`` or absent).
    """
    from msai.services.nautilus.security_master.live_resolver import (
        AssetClass,
        ResolvedInstrument,
    )

    call_log: list[Any] = []

    async def fake_lookup_for_live(
        symbols: list[str],
        **kwargs: Any,
    ) -> list[ResolvedInstrument]:
        call_log.append(kwargs.get("asset_class"))
        return [
            ResolvedInstrument(
                canonical_id="SPY.IBKR",
                asset_class=AssetClass.EQUITY,
                contract_spec={"symbol": "SPY", "primaryExchange": "ARCA"},
                effective_window=(date(2020, 1, 1), None),
            )
        ]

    monkeypatch.setattr(
        "msai.services.symbology_shim.lookup_for_live",
        fake_lookup_for_live,
    )

    await resolve_for_databento(
        InstrumentId.from_str("SPY.IBKR"),
        session=None,  # type: ignore[arg-type]
        as_of_date=date(2026, 5, 29),
    )

    assert call_log == [None], (
        f"first lookup must be unpinned so non-equity members raise "
        f"NotImplementedError; got {call_log!r}"
    )


@pytest.mark.asyncio
async def test_resolve_for_databento_falls_back_to_equity_on_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex iter 11 P2 regression: when the unpinned lookup raises
    ``AmbiguousRegistryError`` (e.g. ``SPY`` exists as both an ARCA
    equity AND option rows), the shim retries with
    ``asset_class=AssetClass.EQUITY`` to disambiguate. PR 1 is
    equities-only so the equity pin is the correct narrowing."""
    from msai.services.nautilus.security_master.live_resolver import (
        AmbiguityReason,
        AmbiguousRegistryError,
        AssetClass,
        ResolvedInstrument,
    )

    call_log: list[Any] = []

    async def fake_lookup_for_live(
        symbols: list[str],
        **kwargs: Any,
    ) -> list[ResolvedInstrument]:
        ac = kwargs.get("asset_class")
        call_log.append(ac)
        if ac is None:
            raise AmbiguousRegistryError(
                symbol="SPY",
                conflicts=["equity", "option"],
                reason=AmbiguityReason.CROSS_ASSET_CLASS,
            )
        return [
            ResolvedInstrument(
                canonical_id="SPY.IBKR",
                asset_class=AssetClass.EQUITY,
                contract_spec={"symbol": "SPY", "primaryExchange": "ARCA"},
                effective_window=(date(2020, 1, 1), None),
            )
        ]

    monkeypatch.setattr(
        "msai.services.symbology_shim.lookup_for_live",
        fake_lookup_for_live,
    )

    await resolve_for_databento(
        InstrumentId.from_str("SPY.IBKR"),
        session=None,  # type: ignore[arg-type]
        as_of_date=date(2026, 5, 29),
    )

    assert call_log == [None, AssetClass.EQUITY], (
        f"expected unpinned-then-EQUITY-fallback sequence; got {call_log!r}"
    )


@pytest.mark.asyncio
async def test_resolve_for_databento_retries_with_dotted_canonical_on_alias_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex iter 22 P2: when the bare canonical symbol misses (e.g.
    registry raw_symbol=``GOOG`` with active alias ``GOOGL.NASDAQ`` —
    canonical ``GOOGL.IBKR`` doesn't match raw_symbol ``GOOG``), the
    shim retries with the FULL dotted canonical so the resolver's
    alias-lookup branch fires. Without this, aliased canonicals would
    fall through to the legacy IB-data path."""
    from msai.services.nautilus.security_master.live_resolver import (
        AssetClass,
        RegistryMissError,
        ResolvedInstrument,
    )

    call_log: list[Any] = []

    async def fake_lookup_for_live(
        symbols: list[str],
        **kwargs: Any,
    ) -> list[ResolvedInstrument]:
        sym = symbols[0]
        call_log.append(sym)
        # Bare ``GOOGL`` misses; dotted ``GOOGL.IBKR`` hits via alias.
        if sym == "GOOGL":
            raise RegistryMissError(
                symbols=["GOOGL"],
                as_of_date=date(2026, 5, 30),
            )
        return [
            ResolvedInstrument(
                canonical_id="GOOGL.IBKR",
                asset_class=AssetClass.EQUITY,
                contract_spec={"symbol": "GOOG", "primaryExchange": "NASDAQ"},
                effective_window=(date(2020, 1, 1), None),
            )
        ]

    monkeypatch.setattr(
        "msai.services.symbology_shim.lookup_for_live",
        fake_lookup_for_live,
    )

    target = await resolve_for_databento(
        InstrumentId.from_str("GOOGL.IBKR"),
        session=None,  # type: ignore[arg-type]
        as_of_date=date(2026, 5, 30),
    )

    assert call_log == ["GOOGL", "GOOGL.IBKR"], (
        f"expected bare-then-dotted lookup sequence; got {call_log!r}"
    )
    # The actual Databento subscription uses the resolver's contract_spec
    # ``symbol`` (e.g. ``GOOG``), not the strategy's canonical alias.
    assert target.native_symbol == "GOOG"
    assert target.native_venue == "XNAS"


# --- DatabentoSubscriptionTarget dataclass shape ------------------------


def test_databento_subscription_target_is_frozen_dataclass() -> None:
    import dataclasses

    target = DatabentoSubscriptionTarget(
        dataset="EQUS.MINI",
        native_symbol="AAPL",
        native_venue="XNAS",
    )
    # frozen=True → attribute assignment raises FrozenInstanceError.
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.dataset = "OTHER"  # type: ignore[misc]
