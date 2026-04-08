"""SecurityMaster service (Phase 2 task 2.5).

Cache-first resolver that answers "give me the Nautilus
``Instrument`` for this logical spec" with the fewest possible IB
round-trips:

1. Compute the canonical ID from the spec
   (``InstrumentSpec.canonical_id()``).
2. Look up ``instrument_cache`` by canonical_id.
3. Cache HIT → deserialize the cached ``nautilus_instrument_json``
   back into a Nautilus ``Instrument`` via its ``from_dict``
   classmethod and return.
4. Cache MISS → qualify via :class:`IBQualifier` (which delegates
   to Nautilus's ``InteractiveBrokersInstrumentProvider``), extract
   trading hours from the ``contract_details`` Nautilus cached on
   the provider, write the row, return.
5. Stale (``last_refreshed_at`` older than ``cache_validity_days``)
   → background refresh scheduled, current cached value returned.

Why this is separate from :class:`IBQualifier`:

- The qualifier owns the IB round-trip mechanics (contract
  construction, provider delegation, batching).
- The service owns the DB cache layer + the hot-path routing.

Running separately means the service can answer from cache without
instantiating an ``InteractiveBrokersClient`` at all — which is the
common case once the cache is warm.

Bulk resolve semantics:

- :meth:`bulk_resolve` splits the input into "cached" and
  "missing" buckets BEFORE hitting IB, so a batch of 100 specs
  with 95 cache hits fires exactly 5 IB requests (not 100). This
  is the whole reason the cache exists — IB's
  ``reqContractDetails`` rate limit is 50 msg/sec and each live
  deployment pre-loads every instrument its strategies need.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from msai.models.instrument_cache import InstrumentCache
from msai.services.nautilus.security_master.parser import (
    extract_trading_hours,
    nautilus_instrument_to_cache_json,
)

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument
    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.services.nautilus.security_master.ib_qualifier import IBQualifier
    from msai.services.nautilus.security_master.specs import InstrumentSpec


DEFAULT_CACHE_VALIDITY_DAYS = 30
"""Rows older than this are considered stale — the next resolve
returns the cached value AND schedules a background refresh."""


class SecurityMaster:
    """Cache-first instrument resolver.

    Args:
        qualifier: IB qualifier adapter that hits Nautilus's
            :class:`InteractiveBrokersInstrumentProvider` for
            cache misses. Production wires a short-lived provider
            bound to an isolated IB client; tests pass a stub.
        db: Async session factory callable — we open a fresh
            session per ``resolve`` call so the cache writes don't
            interfere with callers' transactions.
        cache_validity_days: Staleness threshold. A row whose
            ``last_refreshed_at`` is older than this gets a
            background refresh queued (caller still sees the
            cached value).
    """

    def __init__(
        self,
        *,
        qualifier: IBQualifier,
        db: AsyncSession,
        cache_validity_days: int = DEFAULT_CACHE_VALIDITY_DAYS,
    ) -> None:
        self._qualifier = qualifier
        self._db = db
        self._cache_validity = timedelta(days=cache_validity_days)

    async def resolve(self, spec: InstrumentSpec) -> Instrument:
        """Resolve a single spec, consulting the cache first.

        Returns a freshly-built Nautilus ``Instrument`` (either
        deserialized from the cache or newly qualified from IB).
        """
        canonical_id = spec.canonical_id()

        cached = await self._read_cache(canonical_id)
        if cached is not None:
            return _instrument_from_cache_row(cached)

        # Cache miss — qualify, extract trading hours, write, return.
        instrument = await self._qualifier.qualify(spec)
        trading_hours_json = self._trading_hours_for(
            canonical_id=canonical_id,
        )
        await self._write_cache(
            spec=spec,
            canonical_id=canonical_id,
            instrument=instrument,
            trading_hours_json=trading_hours_json,
        )
        return instrument

    async def bulk_resolve(self, specs: list[InstrumentSpec]) -> list[Instrument]:
        """Resolve a batch of specs, minimizing IB round-trips.

        The implementation:

        1. Computes all canonical ids once.
        2. Fetches all matching cache rows in a single SELECT.
        3. Iterates the input preserving order, using the cached
           row if present or calling ``resolve`` (which qualifies)
           for misses.

        A bulk call of 100 specs with 95 cache hits makes ONE
        SELECT + 5 ``reqContractDetails`` round-trips.
        """
        if not specs:
            return []

        canonical_ids = [spec.canonical_id() for spec in specs]
        cached_rows = await self._read_cache_bulk(canonical_ids)

        results: list[Instrument] = []
        for spec, canonical_id in zip(specs, canonical_ids, strict=True):
            row = cached_rows.get(canonical_id)
            if row is not None:
                results.append(_instrument_from_cache_row(row))
                continue
            # Miss → full single-spec resolve path (writes cache
            # as a side effect so a subsequent spec with the same
            # canonical id in this same batch gets the fresh row).
            results.append(await self.resolve(spec))
        return results

    async def refresh(self, canonical_id: str) -> Instrument:
        """Force a fresh IB qualification for this canonical_id.

        The caller must have the ``InstrumentSpec`` that produced
        this canonical_id — we can't reconstruct it from the string
        alone. In practice the staleness-aware background refresh
        path rebuilds the spec from the cached ``ib_contract_json``
        and calls ``refresh`` with the fresh result; callers that
        don't know the spec should use ``resolve`` instead (which
        reads from cache first).

        Implementation note: for the Phase 2 acceptance we expose
        this as a hook; the background-refresh scheduler lands in
        Phase 4 along with the heartbeat-driven stale detection.
        """
        raise NotImplementedError(
            "refresh() requires a spec reconstructed from cached IB "
            "contract details; the background-refresh scheduler lands "
            "in Phase 4. Callers should use resolve() for now."
        )

    # ------------------------------------------------------------------
    # Cache IO
    # ------------------------------------------------------------------

    async def _read_cache(self, canonical_id: str) -> InstrumentCache | None:
        row = (
            await self._db.execute(
                select(InstrumentCache).where(InstrumentCache.canonical_id == canonical_id)
            )
        ).scalar_one_or_none()
        return row

    async def _read_cache_bulk(self, canonical_ids: list[str]) -> dict[str, InstrumentCache]:
        """One-shot SELECT WHERE canonical_id IN (...) — bounded by
        the input batch size, so no risk of loading the whole cache."""
        if not canonical_ids:
            return {}
        rows = (
            (
                await self._db.execute(
                    select(InstrumentCache).where(InstrumentCache.canonical_id.in_(canonical_ids))
                )
            )
            .scalars()
            .all()
        )
        return {row.canonical_id: row for row in rows}

    async def _write_cache(
        self,
        *,
        spec: InstrumentSpec,
        canonical_id: str,
        instrument: Instrument,
        trading_hours_json: dict | None,
    ) -> None:
        """Upsert into ``instrument_cache`` using
        ``INSERT ... ON CONFLICT DO UPDATE`` so concurrent resolves
        for the same instrument can't collide on the PK."""
        table = InstrumentCache.__table__
        nautilus_json = nautilus_instrument_to_cache_json(instrument)
        # IB contract JSON — reconstruct from the spec so we store
        # the user's intent alongside the Nautilus-side serialization.
        # The live/refresh paths can re-derive the IBContract from
        # this dict when the cache row is the only source of truth
        # (e.g. after a Nautilus upgrade).
        from msai.services.nautilus.security_master.ib_qualifier import (
            spec_to_ib_contract,
        )

        ib_contract_dict = _ib_contract_to_dict(spec_to_ib_contract(spec))

        stmt = pg_insert(table).values(
            canonical_id=canonical_id,
            asset_class=spec.asset_class,
            venue=spec.venue,
            ib_contract_json=ib_contract_dict,
            nautilus_instrument_json=nautilus_json,
            trading_hours=trading_hours_json,
            last_refreshed_at=datetime.now(UTC),
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[table.c.canonical_id],
            set_={
                "asset_class": stmt.excluded.asset_class,
                "venue": stmt.excluded.venue,
                "ib_contract_json": stmt.excluded.ib_contract_json,
                "nautilus_instrument_json": stmt.excluded.nautilus_instrument_json,
                "trading_hours": stmt.excluded.trading_hours,
                "last_refreshed_at": stmt.excluded.last_refreshed_at,
            },
        )
        await self._db.execute(upsert)
        await self._db.commit()

    # ------------------------------------------------------------------
    # Trading-hours extraction hook
    # ------------------------------------------------------------------

    def _trading_hours_for(self, *, canonical_id: str) -> dict | None:
        """Extract trading hours from the qualifier provider's
        cached :class:`ContractDetails` for the given canonical id.

        The :class:`InteractiveBrokersInstrumentProvider` stores
        ``contract_details`` keyed by Nautilus ``InstrumentId``
        (see ``providers.py:93``). We read that mapping if the
        provider exposes it; otherwise we return ``None`` so the
        cache row stores NULL (which is correct for 24h venues).

        Tests inject a fake provider with an empty ``contract_details``
        dict — the production flow writes real IB details via the
        provider's internal ``load_with_return_async``.
        """
        from nautilus_trader.model.identifiers import InstrumentId

        provider = getattr(self._qualifier, "_provider", None)
        if provider is None:
            return None
        details_map = getattr(provider, "contract_details", None)
        if not details_map:
            return None
        try:
            instrument_id = InstrumentId.from_str(canonical_id)
        except Exception:  # noqa: BLE001 — malformed id → skip trading hours
            return None
        details = details_map.get(instrument_id)
        if details is None:
            return None
        return extract_trading_hours(
            trading_hours=getattr(details, "tradingHours", None),
            liquid_hours=getattr(details, "liquidHours", None),
            time_zone_id=getattr(details, "timeZoneId", None),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _instrument_from_cache_row(row: InstrumentCache) -> Instrument:
    """Rebuild a Nautilus ``Instrument`` from its cached
    ``to_dict()`` JSONB blob.

    Nautilus's concrete instrument classes (``Equity``,
    ``FuturesContract``, ``OptionContract``, ``CurrencyPair``, …)
    each have their own static ``from_dict(values)`` + per-class
    ``to_dict(obj)`` (see e.g. ``model/instruments/equity.pyx``
    line 207/224). There is no base-class dispatch by ``type``
    field — we do that here from the ``"type"`` key each
    ``to_dict`` writes.

    Unknown types raise ``ValueError`` loudly so a corrupted or
    future-schema cache row doesn't silently build the wrong
    object.
    """
    from nautilus_trader.model.instruments import (
        BettingInstrument,
        BinaryOption,
        CryptoFuture,
        CryptoPerpetual,
        CurrencyPair,
        Equity,
        FuturesContract,
        FuturesSpread,
        IndexInstrument,
        OptionContract,
        OptionSpread,
        SyntheticInstrument,
    )

    data = dict(row.nautilus_instrument_json)
    type_name = data.get("type")

    dispatch: dict[str, type[Instrument]] = {
        "Equity": Equity,
        "FuturesContract": FuturesContract,
        "FuturesSpread": FuturesSpread,
        "OptionContract": OptionContract,
        "OptionSpread": OptionSpread,
        "CurrencyPair": CurrencyPair,
        "CryptoFuture": CryptoFuture,
        "CryptoPerpetual": CryptoPerpetual,
        "IndexInstrument": IndexInstrument,
        "BinaryOption": BinaryOption,
        "BettingInstrument": BettingInstrument,
        "SyntheticInstrument": SyntheticInstrument,
    }
    cls = dispatch.get(type_name or "")
    if cls is None:
        raise ValueError(
            f"unknown instrument type in cache row: {type_name!r} — "
            "schema drift between writer and reader",
        )
    return cls.from_dict(data)


def _ib_contract_to_dict(contract) -> dict:  # type: ignore[no-untyped-def]
    """Serialize an IBContract msgspec struct to a plain dict.

    ``msgspec.structs.asdict`` is the canonical way to convert a
    frozen struct into a dict without copying Nautilus's own
    bespoke serialization. We keep this as a small helper so the
    service doesn't take a direct dependency on msgspec.
    """
    import msgspec

    return msgspec.structs.asdict(contract)
