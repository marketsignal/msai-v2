"""Bidirectional Databento ↔ IBKR symbology shim.

Strategies in MSAI v2 always speak the canonical ``<SYM>.IBKR`` venue suffix
(per ``nautilus.md`` architectural rule #3 — "Pin venue names per
environment"). The Databento data adapter, however, emits bars tagged with
the underlying *data-source* venue (``XNAS``, ``GLBX``, …). This shim
provides:

* **Outbound** (``resolve_for_databento``): translates a strategy-canonical
  ``InstrumentId`` into the ``(dataset, native_symbol, native_venue)``
  triple that the Databento subscription factory needs. Delegates to the
  existing ``lookup_for_live`` registry resolver and maps ``AssetClass`` →
  Databento dataset / venue.

* **Inbound** (``retag_inbound_bar``): rewrites an incoming Databento
  ``Bar`` so its ``bar_type.instrument_id.venue == Venue("IBKR")``. Audit
  metadata (original venue, dataset, provider, native symbol) is forwarded
  to a caller-supplied sink so the provenance isn't lost in the re-tag.

The shim sits **outside** the Nautilus Databento adapter (research brief §2
— Nautilus' built-in ``venue_dataset_map`` is a dataset *alias*, not a
venue rename). T11 wires it into the live data pipeline via
``SymbologyShimActor``; this module is purely pure / async-but-stateless
business logic so it can be unit-tested without spinning a Nautilus node.

**PR 1 scope is equities-only** (council 2026-05-29). Futures are out of
scope because ``contract_spec["symbol"]`` from the resolver carries the
root (``"ES"``) — NOT the active contract month (``"ESM6"``) — and the
contract-binding lives in ``canonical_id``/``alias`` rather than the dict.
Bridging that gap for futures is deferred to a follow-up PR; for now
``resolve_for_databento`` raises :class:`NotImplementedError` for any
asset class other than ``EQUITY``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId, Venue

# AssetClass is defined in ``live_resolver.py`` (NOT ``specs.py``).
# Members: EQUITY, FUTURES (plural), FX, OPTION, CRYPTO.
from msai.services.nautilus.security_master.live_resolver import (
    AmbiguousRegistryError,
    AssetClass,
    RegistryMissError,
    ResolvedInstrument,
    lookup_for_live,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession


# Canonical strategy-side venue. Pinned per ``nautilus.md`` architectural
# rule #3 — strategies always see ``<SYM>.IBKR`` in backtest AND live.
_IBKR_VENUE: Venue = Venue("IBKR")

# AssetClass enum member → Databento dataset name.
# PR 1 scope is EQUITIES ONLY (council 2026-05-29 ratified). Futures /
# option / fx entries are intentionally NOT enumerated here; the explicit
# ``NotImplementedError`` below points implementers at the deferred work.
_ASSET_CLASS_TO_DATASET: dict[AssetClass, str] = {
    # ``DBEQ.BASIC`` was DEPRECATED by Databento on 2025-01-13 and split into
    # the individually-licensed ``EQUS.*`` datasets — a live subscription to it
    # connects but yields ZERO instrument definitions and ZERO bars (confirmed
    # in the 2026-06-01 real-money drill: "Timeout waiting for instruments" +
    # no data). ``EQUS.MINI`` (Databento US Equities Mini) is the live-active,
    # zero-license-fee consolidated top-of-book + last-sale feed covering all
    # US listing venues, and is what historical ingestion already uses.
    AssetClass.EQUITY: "EQUS.MINI",
    # AssetClass.FUTURES: "GLBX.MDP3",   # deferred — root vs contract symbol ambiguity
    # AssetClass.OPTION:  "OPRA.PILLAR", # deferred
    # AssetClass.FX:      TBD            # not in PR 1 scope
}

# Listing venue (from ``ResolvedInstrument.contract_spec['primaryExchange']``)
# → Databento NATIVE venue suffix. This split (Codex iter 3 F2) replaces
# the prior hardcoded ``XNAS`` for every equity — NYSE-listed names
# (``GE.NYSE``, ``SPY.ARCA``, ``BRK.B.NYSE``) need their Databento
# subscriptions tagged with the correct native venue, otherwise the
# Databento client builds ``GE.XNAS`` (wrong) and the shim's
# ``venue_dataset_map`` keys on ``XNAS`` for non-NASDAQ instruments
# (also wrong). All four entries resolve to the ``EQUS.MINI`` dataset (the
# live-active US Equities Mini bundle; ``DBEQ.BASIC`` was deprecated
# 2025-01-13), but the venue suffix differs.
#
# Keep in sync with ``live_resolver._build_contract_spec`` equity branch
# (``primaryExchange = definition.listing_venue``) — any listing venue
# the registry can produce MUST appear here or fail fast.
_LISTING_VENUE_TO_DATABENTO_NATIVE: dict[str, str] = {
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "ARCA": "ARCX",
    "AMEX": "XASE",
}


class UnsupportedListingVenueError(ValueError):
    """Raised when an EQUITY resolves but its listing venue isn't in
    ``_LISTING_VENUE_TO_DATABENTO_NATIVE`` (BATS / OTC / international).

    Subclasses :class:`ValueError` (Codex iter 19 P2) so the
    supervisor's payload-factory error classifier in
    ``FleetRouter.spawn()`` treats it as PERMANENT
    (``SPAWN_FAILED_PERMANENT`` — no PEL retry). ``Exception`` would
    land in the transient branch and let the supervisor retry forever.

    Distinct from :class:`NotImplementedError` (which signals the
    intended non-equity fallback path in the supervisor) and from
    :class:`LiveResolverError` (registry-side resolution failures —
    also a ``ValueError`` subclass). The supervisor's per-account
    fallback catch is narrowed to those two; this error propagates
    past it as a hard permanent failure so operators extend the
    mapping table instead of silently downgrading to the legacy
    IB-data topology. Codex iter 18 P2 + iter 19 P2.
    """


@dataclass(frozen=True)
class DatabentoSubscriptionTarget:
    """The ``(dataset, native_symbol, native_venue)`` triple the Databento
    subscription factory needs to register a feed."""

    dataset: str
    native_symbol: str
    native_venue: str


async def resolve_for_databento(
    canonical: InstrumentId,
    *,
    session: AsyncSession,
    as_of_date: date,
) -> DatabentoSubscriptionTarget:
    """Map a strategy-canonical ``<SYM>.IBKR`` to the Databento triple.

    Delegates to :func:`lookup_for_live` (the existing async live-resolution
    function at ``live_resolver.py:447``) and maps the resolver's
    ``AssetClass`` onto the Databento dataset + native venue.

    Args:
        canonical: A canonical strategy-side identifier whose venue MUST be
            ``IBKR``. The bare symbol is forwarded to ``lookup_for_live``.
        session: Async DB session (forwarded to the resolver).
        as_of_date: Exchange-local date (forwarded to the resolver — used
            for futures-roll windowing in the registry).

    Returns:
        :class:`DatabentoSubscriptionTarget` with the dataset string
        (e.g. ``"EQUS.MINI"``), the native symbol the data feed uses
        (e.g. ``"AAPL"``), and the native venue (e.g. ``"XNAS"``).

    Raises:
        ValueError: ``canonical.venue`` is not ``IBKR``, or the resolver
            returns an asset class that has no Databento mapping table
            entry. Any error class from :func:`lookup_for_live`
            (``RegistryMissError``, ``RegistryIncompleteError``,
            ``AmbiguousRegistryError``, ``UnsupportedAssetClassError``)
            propagates unchanged — all are subclasses of ``ValueError``.
        NotImplementedError: The resolver returned a non-equity asset
            class. PR 1 scope is equities-only; futures shim semantics
            are deferred to a follow-up PR.
    """
    if canonical.venue != _IBKR_VENUE:
        raise ValueError(
            "resolve_for_databento expects canonical .IBKR venue; "
            f"got {canonical.venue} (full id: {canonical})"
        )

    # Codex iter 12 P2 #1: try UNPINNED first so non-equity members
    # (e.g. an ES futures strategy that landed here by mistake) raise
    # ``NotImplementedError`` from the asset-class guard below instead of
    # being mis-resolved as the equity row of the same root. Only fall
    # back to ``asset_class=EQUITY`` when the unpinned lookup hits
    # cross-class ambiguity (iter 11 P2 — e.g. ``SPY`` as both an ARCA
    # equity AND option rows in the registry).
    #
    # Codex iter 22 P2: for aliased canonical symbols (e.g. registry
    # raw_symbol=``GOOG`` with active alias ``GOOGL.NASDAQ``), the bare
    # symbol miss on ``GOOGL`` doesn't trigger ``find_by_alias`` because
    # the resolver only attempts alias lookup when the symbol contains
    # a ``.`` (live_resolver.py:527). Fall back to the FULL dotted
    # canonical (``GOOGL.IBKR``) on RegistryMiss so the alias-lookup
    # path fires. The registry must store an ``.IBKR`` alias entry for
    # any aliased canonical (project convention) — verified by the
    # integration test ``test_outbound_aliased_canonical_resolves_via_ibkr_alias``.
    raw_symbol_key = str(canonical.symbol)
    canonical_dotted_key = str(canonical)
    try:
        resolved_items: list[ResolvedInstrument] = await lookup_for_live(
            [raw_symbol_key],
            as_of_date=as_of_date,
            session=session,
        )
    except AmbiguousRegistryError:
        resolved_items = await lookup_for_live(
            [raw_symbol_key],
            as_of_date=as_of_date,
            session=session,
            asset_class=AssetClass.EQUITY,
        )
    except RegistryMissError:
        # Aliased canonical: retry with the dotted ``.IBKR`` form so the
        # resolver's alias-lookup branch fires. Propagate any error from
        # this attempt — the original miss is preserved as the cause.
        resolved_items = await lookup_for_live(
            [canonical_dotted_key],
            as_of_date=as_of_date,
            session=session,
        )
    resolved = resolved_items[0]

    # PR 1 scope: equities only (council 2026-05-29). Fail fast on anything
    # else so callers can't silently emit incorrect subscriptions for
    # futures roots / options / fx pairs. The supervisor catches
    # ``NotImplementedError`` and falls back to the legacy single-gateway
    # builder (per-account topology activation only fires when ALL
    # resolved instruments are equities).
    if resolved.asset_class != AssetClass.EQUITY:
        raise NotImplementedError(
            "resolve_for_databento PR 1 scope is equities only; "
            f"got asset_class={resolved.asset_class.value!r} for {canonical}. "
            "Futures shim semantics deferred to a follow-up PR — "
            "contract_spec['symbol'] carries the root (e.g. 'ES'), not the "
            "active contract month (e.g. 'ESM6'); the contract binding "
            "lives in canonical_id / alias and needs explicit bridge work."
        )

    # Defensive — the equity branch above guarantees this lookup hits, but
    # keep the explicit check so any future mapping-table edit that adds
    # an asset class to the enum without a Databento entry surfaces here
    # (instead of KeyError).
    if resolved.asset_class not in _ASSET_CLASS_TO_DATASET:
        raise ValueError(
            f"no Databento dataset mapping for asset_class={resolved.asset_class.value!r}"
        )
    dataset = _ASSET_CLASS_TO_DATASET[resolved.asset_class]

    # ``contract_spec["symbol"]`` is filled by ``_build_contract_spec`` at
    # ``live_resolver.py:332`` (equity branch) — it carries the
    # operator-typed raw symbol (e.g. "AAPL"). Cast to str defensively so
    # downstream string concatenation can't trip on non-string values.
    native_symbol_raw = resolved.contract_spec.get("symbol")
    if not native_symbol_raw:
        # Defensive: equity branch of _build_contract_spec always sets
        # this key, but make the failure mode explicit if the spec shape
        # changes upstream.
        raise ValueError(
            f"resolver returned contract_spec without 'symbol' key for "
            f"{canonical}: {resolved.contract_spec!r}"
        )

    # Codex iter 3 F2: derive the Databento NATIVE venue from the RESOLVED
    # listing venue (``contract_spec['primaryExchange']``) — not from the
    # asset class alone. Previously every equity got ``XNAS`` regardless of
    # actual listing, so NYSE-listed names produced wrong subscriptions
    # (``GE.XNAS``) and broke the supervisor's ``venue_dataset_map`` keys.
    # The four US listing venues mapped here match the ones the registry
    # emits via ``live_resolver._build_contract_spec`` equity branch.
    listing_venue_raw = resolved.contract_spec.get("primaryExchange")
    if not listing_venue_raw:
        raise ValueError(
            f"resolver returned contract_spec without 'primaryExchange' key "
            f"for {canonical}: {resolved.contract_spec!r}"
        )
    listing_venue = str(listing_venue_raw)
    native_venue = _LISTING_VENUE_TO_DATABENTO_NATIVE.get(listing_venue)
    if native_venue is None:
        # Codex iter 18 P2: a missing Databento venue mapping for an
        # equity (e.g. BATS / OTC / international listing) is an
        # OPERATOR CONFIG error, NOT the "non-equity passthrough" path.
        # Raise ``UnsupportedListingVenueError`` (a plain ``Exception``,
        # NOT ``NotImplementedError`` / ``LiveResolverError``) so the
        # supervisor's fallback catch CAN'T swallow it — the deployment
        # must fail loud so the operator extends the mapping table
        # instead of silently downgrading to the legacy IB-data path.
        raise UnsupportedListingVenueError(
            "resolve_for_databento has no Databento native-venue mapping "
            f"for listing_venue={listing_venue!r} (canonical {canonical}). "
            "PR 1 supports NASDAQ/NYSE/ARCA/AMEX only — extend "
            "_LISTING_VENUE_TO_DATABENTO_NATIVE before adding OTC / "
            "international equity listings."
        )

    return DatabentoSubscriptionTarget(
        dataset=dataset,
        native_symbol=str(native_symbol_raw),
        native_venue=native_venue,
    )


def retag_inbound_bar(
    bar: Bar,
    *,
    audit_metadata_sink: Callable[[dict[str, Any]], None] | None = None,
) -> Bar:
    """Re-tag a Databento ``Bar``'s venue suffix to IBKR.

    Nautilus ``Bar`` is frozen — this returns a new instance whose
    ``bar_type.instrument_id.venue == Venue("IBKR")``. All other fields
    (symbol, bar specification, aggregation source, OHLCV values, and
    timestamps) are preserved verbatim.

    When ``audit_metadata_sink`` is provided, the callable receives a dict
    with the keys::

        {
            "provider": "databento",
            "dataset": <databento dataset string from the mapping table>,
            "native_venue": <original venue, e.g. "XNAS">,
            "native_symbol": <original symbol, e.g. "AAPL">,
            "original_instrument_id": <stringified original InstrumentId>,
            "ts_event": <ns since epoch>,
        }

    This keeps the provenance auditable without leaking it onto the Bar
    itself (which downstream Nautilus components consume as the canonical
    instrument id). T11's ``SymbologyShimActor`` wires the sink to the
    project's audit log.

    Args:
        bar: An incoming Databento ``Bar`` whose ``bar_type.instrument_id``
            has the native data-source venue.
        audit_metadata_sink: Optional callback for emitting audit metadata.
            Called synchronously exactly once per call; exceptions raised
            by the sink propagate (callers wrap as needed).

    Returns:
        A new ``Bar`` with the venue rewritten to IBKR. If the bar is
        already tagged IBKR, the original is returned unchanged
        (no-op fast path); the audit sink is NOT called in that case.
    """
    original_id: InstrumentId = bar.bar_type.instrument_id
    if original_id.venue == _IBKR_VENUE:
        # Already canonical — nothing to re-tag, nothing to audit.
        return bar

    # Build the IBKR-tagged BarType. Symbol + bar specification +
    # aggregation source are preserved verbatim.
    new_instrument_id = InstrumentId(original_id.symbol, _IBKR_VENUE)
    new_bar_type = BarType(
        new_instrument_id,
        bar.bar_type.spec,
        bar.bar_type.aggregation_source,
    )

    # Bar is frozen; the constructor takes positional bar_type + OHLCV +
    # volume + ts_event + ts_init (verified against
    # ``nautilus_trader.model.data.Bar`` in the venv at 1.223.0).
    # Codex code-review iter 1 P2: preserve ``is_revision`` so corrections
    # don't get treated as new bars by the data engine. Default to False
    # only when the source bar's flag is missing (legacy synthetic bars).
    new_bar = Bar(
        new_bar_type,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.ts_event,
        bar.ts_init,
        is_revision=getattr(bar, "is_revision", False),
    )

    if audit_metadata_sink is not None:
        native_venue = str(original_id.venue)
        # Look up the dataset by reverse-mapping native_venue. PR 1 maps
        # all US equity listing venues (XNAS / XNYS / ARCX / XASE) to the
        # ``EQUS.MINI`` dataset. Anything
        # outside that set yields ``dataset=None`` in the audit metadata
        # so the gap is observable (matches the legacy behavior).
        # Codex iter 3 F2: keep this reverse lookup keyed on the SAME
        # _LISTING_VENUE_TO_DATABENTO_NATIVE table the outbound path
        # consults — if a new equity listing is added there, the audit
        # path picks up its dataset automatically via this reverse walk.
        dataset: str | None = None
        if native_venue in _LISTING_VENUE_TO_DATABENTO_NATIVE.values():
            # All currently-mapped US equity native venues use EQUS.MINI.
            dataset = _ASSET_CLASS_TO_DATASET[AssetClass.EQUITY]
        audit_metadata_sink(
            {
                "provider": "databento",
                "dataset": dataset,
                "native_venue": native_venue,
                "native_symbol": str(original_id.symbol),
                "original_instrument_id": str(original_id),
                "ts_event": bar.ts_event,
            }
        )

    return new_bar
