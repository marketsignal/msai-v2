"""Unit tests for :class:`SymbologyShimActor` (PR 1 T11 load-bearing).

The actor has TWO responsibilities and BOTH are tested here:

1. **Outbound subscription on start** — ``on_start`` MUST call
   ``subscribe_bars`` for every native bar type in the configured
   mapping. Without this, the Databento data client never starts a
   feed and the rest of the pipeline starves (Codex iter 7 P1).

2. **Inbound retag + republish** — when a native XNAS bar arrives on
   the bus, the actor publishes a retagged ``.IBKR`` bar onto the
   canonical bar topic where the strategy's subscription picks it up
   (Codex iter 6 P1-2).

These tests exercise the Python surface of the actor directly —
``monkeypatch`` is sufficient for the outbound test (we only need to
observe that ``subscribe_bars`` was called with the right BarTypes);
the inbound test wires a real Nautilus :class:`MessageBus` so we can
verify the full retag + publish path end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest  # noqa: TC002 — pytest is conventionally a runtime test import even when only annotations use it
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.portfolio.portfolio import Portfolio

from msai.services.symbology_shim_actor import (
    SymbologyShimActor,
    SymbologyShimActorConfig,
)


def _register_actor_with_bus(actor: SymbologyShimActor, bus: MessageBus) -> None:
    """Register the actor against a real MessageBus + real (but minimal) Nautilus deps.

    Nautilus's ``Actor.msgbus`` is a Cython read-only property, so the
    only way to inject a bus is via ``register_base(portfolio, msgbus,
    cache, clock)``. ``portfolio`` is type-checked as
    :class:`PortfolioFacade` (Cython), so we instantiate the real
    :class:`Portfolio` (which requires a msgbus + cache + clock — same
    pattern as Nautilus's own ``test_kit.stubs.component.TestComponentStubs.portfolio``).
    """
    clock = LiveClock()
    cache = Cache()
    portfolio = Portfolio(msgbus=bus, cache=cache, clock=clock)
    actor.register_base(
        portfolio=portfolio,
        msgbus=bus,
        cache=cache,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


_BAR_TYPE_MAP: dict[str, str] = {
    "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
}
_VENUE_DATASET_MAP: dict[str, str] = {"XNAS": "EQUS.MINI"}


def _build_actor() -> SymbologyShimActor:
    return SymbologyShimActor(
        config=SymbologyShimActorConfig(
            canonical_to_native_bar_types=_BAR_TYPE_MAP,
            venue_dataset_map=_VENUE_DATASET_MAP,
        ),
    )


def _build_native_bar() -> Bar:
    """Build a synthetic XNAS bar matching the native side of the map."""
    bar_type = BarType.from_str("AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
    return Bar(
        bar_type,
        Price.from_str("200.00"),
        Price.from_str("201.00"),
        Price.from_str("199.00"),
        Price.from_str("200.50"),
        Quantity.from_str("1000"),
        1_700_000_000_000_000_000,
        1_700_000_000_000_000_001,
    )


# ---------------------------------------------------------------------------
# Outbound — on_start subscribes to native bar types.
# ---------------------------------------------------------------------------


def test_on_start_subscribes_to_native_bar_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex iter 7 P1 (load-bearing): on_start MUST subscribe to the
    NATIVE bar types so Databento (which owns the native venue per
    ``venue_dataset_map``) starts streaming."""
    actor = _build_actor()

    subscribed: list[BarType] = []
    monkeypatch.setattr(actor, "subscribe_bars", subscribed.append)

    actor.on_start()

    assert any(str(bt) == "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL" for bt in subscribed), (
        f"on_start did not subscribe to native bar; got subscribed={subscribed!r}"
    )


def test_on_start_subscribes_one_per_canonical_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_start`` iterates ``canonical_to_native_bar_types.values()`` and
    issues one ``subscribe_bars`` call per entry — multi-symbol universes
    must all be subscribed (not just the first)."""
    multi_map = {
        "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        "MSFT.IBKR-1-MINUTE-LAST-EXTERNAL": "MSFT.XNAS-1-MINUTE-LAST-EXTERNAL",
    }
    actor = SymbologyShimActor(
        config=SymbologyShimActorConfig(
            canonical_to_native_bar_types=multi_map,
            venue_dataset_map=_VENUE_DATASET_MAP,
        ),
    )

    subscribed: list[BarType] = []
    monkeypatch.setattr(actor, "subscribe_bars", subscribed.append)

    actor.on_start()

    subscribed_strs = {str(bt) for bt in subscribed}
    assert subscribed_strs == {
        "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        "MSFT.XNAS-1-MINUTE-LAST-EXTERNAL",
    }


# ---------------------------------------------------------------------------
# Inbound — on_bar retags + republishes on canonical topic.
# ---------------------------------------------------------------------------


def test_on_bar_retags_native_to_ibkr_and_publishes() -> None:
    """Codex iter 6 P1-2 (load-bearing): an incoming native XNAS bar
    must be retagged to ``.IBKR`` and republished onto the canonical
    bar topic. The strategy's bus subscription reads from THIS topic;
    without the republish, no bars ever reach the strategy."""
    actor = _build_actor()

    # Real Nautilus MessageBus — the actor publishes via
    # ``self.msgbus.publish`` so we need a working bus. ``register_base``
    # is the closest-to-prod registration path, but it also expects a
    # Portfolio / Cache; for unit testing we set ``msgbus`` directly
    # (Actor.__init__ leaves it None until registered).
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)

    received: list[Any] = []
    bus.subscribe(
        topic="data.bars.AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
        handler=received.append,
    )

    actor.on_bar(_build_native_bar())

    assert len(received) == 1, (
        f"expected 1 retagged bar on the canonical topic; got {len(received)} "
        f"(received={received!r})"
    )
    retagged = received[0]
    assert str(retagged.bar_type.instrument_id.venue) == "IBKR"
    # The OHLCV must survive the retag verbatim.
    assert str(retagged.open) == "200.00"
    assert str(retagged.close) == "200.50"


def test_on_bar_publishes_with_canonical_bar_type_for_aliased_symbol() -> None:
    """Codex iter 21 P2: for aliased symbols where the native and
    canonical SYMBOLS differ (not just the venue) — e.g. Databento
    emits ``GOOG.XNAS`` but the strategy subscribes to canonical
    ``GOOGL.IBKR`` — the published bar's ``bar_type`` MUST match the
    canonical topic. Otherwise the strategy sees a Bar whose
    ``bar_type.instrument_id`` doesn't match its subscription and
    routing breaks. The actor now rebuilds the Bar from the configured
    canonical ``bar_type`` rather than relying on ``retag_inbound_bar``
    (which only rewrites the venue suffix).
    """
    aliased_map: dict[str, str] = {
        "GOOGL.IBKR-1-MINUTE-LAST-EXTERNAL": "GOOG.XNAS-1-MINUTE-LAST-EXTERNAL",
    }
    actor = SymbologyShimActor(
        config=SymbologyShimActorConfig(
            canonical_to_native_bar_types=aliased_map,
            venue_dataset_map=_VENUE_DATASET_MAP,
        ),
    )
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)

    received: list[Any] = []
    bus.subscribe(
        topic="data.bars.GOOGL.IBKR-1-MINUTE-LAST-EXTERNAL",
        handler=received.append,
    )

    # Inbound bar tagged with the NATIVE symbol+venue.
    native_bar = Bar(
        BarType.from_str("GOOG.XNAS-1-MINUTE-LAST-EXTERNAL"),
        Price.from_str("150.00"),
        Price.from_str("151.00"),
        Price.from_str("149.00"),
        Price.from_str("150.50"),
        Quantity.from_str("500"),
        1_700_000_000_000_000_000,
        1_700_000_000_000_000_001,
    )
    actor.on_bar(native_bar)

    assert len(received) == 1
    published = received[0]
    # Published bar_type MUST match the canonical topic exactly — symbol
    # AND venue. Previously this asserted IBKR venue only; with aliased
    # symbols the symbol part also matters.
    assert str(published.bar_type) == "GOOGL.IBKR-1-MINUTE-LAST-EXTERNAL"
    assert str(published.bar_type.instrument_id) == "GOOGL.IBKR"
    # OHLCV preserved verbatim from the native bar.
    assert str(published.open) == "150.00"
    assert str(published.close) == "150.50"


def test_on_bar_preserves_is_revision_flag_when_republishing() -> None:
    """Codex iter 22 P2: Databento can emit correction bars with
    ``is_revision=True`` (later updates to a previously published bar's
    OHLCV). The actor rebuilds the canonical Bar from raw values for
    aliased-symbol correctness, but MUST forward ``is_revision`` so
    downstream handlers can route corrections distinctly. The Bar
    constructor defaults ``is_revision=False`` — without explicit
    pass-through, corrections would be lost in the republish."""
    actor = _build_actor()
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)

    received: list[Any] = []
    bus.subscribe(
        topic="data.bars.AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
        handler=received.append,
    )

    # Native correction bar — same shape as a normal Bar but with
    # ``is_revision=True``.
    bar_type = BarType.from_str("AAPL.XNAS-1-MINUTE-LAST-EXTERNAL")
    revision_bar = Bar(
        bar_type,
        Price.from_str("200.00"),
        Price.from_str("201.00"),
        Price.from_str("199.00"),
        Price.from_str("200.50"),
        Quantity.from_str("1000"),
        1_700_000_000_000_000_000,
        1_700_000_000_000_000_001,
        True,  # is_revision
    )
    actor.on_bar(revision_bar)

    assert len(received) == 1
    published = received[0]
    assert published.is_revision is True, (
        "republished Bar must preserve is_revision=True so downstream "
        "handlers route corrections distinctly"
    )


def test_on_bar_drops_bar_on_unknown_native_topic() -> None:
    """If a bar arrives whose native bar-type isn't in the configured
    map, the actor logs + drops it. This protects against the case
    where the subscription map drifts between the supervisor and the
    actor — we never want to publish a bar to a topic we can't name."""
    actor = _build_actor()

    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)

    received: list[Any] = []
    bus.subscribe(
        topic="data.bars.AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
        handler=received.append,
    )

    # A bar tagged on a venue (XNYS) that's NOT in the configured map.
    unknown_bar_type = BarType.from_str("FOO.XNYS-1-MINUTE-LAST-EXTERNAL")
    unknown_bar = Bar(
        unknown_bar_type,
        Price.from_str("10.0"),
        Price.from_str("11.0"),
        Price.from_str("9.0"),
        Price.from_str("10.5"),
        Quantity.from_str("100"),
        1_700_000_000_000_000_000,
        1_700_000_000_000_000_001,
    )
    actor.on_bar(unknown_bar)

    assert received == []  # nothing leaked onto the canonical topic


def test_audit_sink_receives_metadata_on_retag() -> None:
    """When a sink is installed via :meth:`set_audit_sink`, it must be
    invoked once per inbound bar with the provenance metadata dict.
    Verifies the actor wires the shim's ``audit_metadata_sink`` argument
    through correctly."""
    actor = _build_actor()
    bus = MessageBus(trader_id=TraderId("TESTER-000"), clock=LiveClock())
    _register_actor_with_bus(actor, bus)

    captured_metadata: list[dict[str, Any]] = []
    actor.set_audit_sink(captured_metadata.append)

    actor.on_bar(_build_native_bar())

    assert len(captured_metadata) == 1
    md = captured_metadata[0]
    assert md["provider"] == "databento"
    assert md["native_venue"] == "XNAS"
    assert md["native_symbol"] == "AAPL"
    assert md["dataset"] == "EQUS.MINI"


# ---------------------------------------------------------------------------
# Inverse map construction
# ---------------------------------------------------------------------------


def test_inverse_map_is_built_at_init_time() -> None:
    """The actor must build the native→canonical inverse map at
    ``__init__`` so the inbound handler is O(1). This is a structural
    test — it exercises the assumption that ``on_bar`` doesn't need
    to walk the forward map on every bar event (a hot path)."""
    actor = _build_actor()
    # Internal field — fine to inspect in unit tests.
    inverse = actor._native_to_canonical  # noqa: SLF001
    assert inverse == {
        "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL": "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
    }
