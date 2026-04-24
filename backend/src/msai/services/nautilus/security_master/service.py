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

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.models.instrument_cache import InstrumentCache
from msai.services.nautilus.security_master.continuous_futures import (
    is_databento_continuous_pattern,
    raw_symbol_from_request,
    resolved_databento_definition,
)
from msai.services.nautilus.security_master.parser import (
    extract_trading_hours,
    nautilus_instrument_to_cache_json,
)
from msai.services.nautilus.security_master.specs import InstrumentSpec

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument
    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.nautilus.security_master.ib_qualifier import IBQualifier


log = get_logger(__name__)


DEFAULT_CACHE_VALIDITY_DAYS = 30
"""Rows older than this are considered stale — the next resolve
returns the cached value AND schedules a background refresh."""


_REGISTRY_TO_INGEST_ASSET_CLASS: dict[str, str] = {
    "equity": "stocks",
    "future": "futures",
    "option": "options",
    "forex": "forex",
    "crypto": "crypto",
    # ``index`` is not currently an ingest-taxonomy key; pass-through
    # so the operator sees the raw value rather than a silent coerce.
    "index": "index",
}
"""Registry/spec-taxonomy (``InstrumentSpec.asset_class``)
→ ingest / Parquet-storage taxonomy. Used by
:meth:`SecurityMaster.asset_class_for_alias` — keep module-level so
callers can inspect the mapping for testing + telemetry without
instantiating a SecurityMaster."""


def compute_advisory_lock_key(provider: str, raw_symbol: str, asset_class: str) -> int:
    """Postgres ``int8`` advisory-lock key derived from a stable blake2b digest.

    Shared between ``_upsert_definition_and_alias`` and the bootstrap
    orchestrator so both paths converge on the same lock when the
    orchestrator pre-acquires before its pre-state SELECT and the upsert
    re-acquires reentrantly. ``blake2b`` (not Python's built-in ``hash()``)
    because ``PYTHONHASHSEED`` is per-process randomized and would drift
    the key across worker restarts.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.blake2b(
        f"{provider}:{raw_symbol}:{asset_class}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, "big", signed=False) & 0x7FFFFFFFFFFFFFFF


_ROLL_SENSITIVE_ROOTS: frozenset[str] = frozenset({"ES"})
"""Raw symbols whose canonical alias changes over time (quarterly
futures roll). For these, ``resolve_for_live``'s warm path B compares
the stored active alias to ``canonical_instrument_id(sym, today=today)``
and falls through to the cold path on mismatch so the roll triggers a
re-qualify. For non-rollable symbols the registry is authoritative."""


class DatabentoDefinitionMissing(Exception):  # noqa: N818 — spec-mandated name (codex parity)
    """Raised by :meth:`SecurityMaster.resolve_for_backtest` when a requested
    symbol has no active registry row under the ``databento`` provider and
    the operator has not pre-warmed the registry.

    Backtests are fail-loud on cold-miss — the error carries the original
    symbol and an operator hint pointing to ``msai instruments refresh``.
    """


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
        qualifier: IBQualifier | None = None,
        db: AsyncSession,
        cache_validity_days: int = DEFAULT_CACHE_VALIDITY_DAYS,
        databento_client: DatabentoClient | None = None,
    ) -> None:
        self._qualifier = qualifier
        self._db = db
        self._cache_validity = timedelta(days=cache_validity_days)
        # Used by the continuous-futures backtest path
        # (``_resolve_databento_continuous``). ``None`` is permitted for
        # live-only callers — a cold-miss on a Databento continuous symbol
        # with ``self._databento is None`` will raise.
        self._databento = databento_client

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
        if self._qualifier is None:
            raise ValueError(
                f"Cache miss for spec {spec!r} requires an IBQualifier — "
                "construct SecurityMaster with qualifier=... or pre-warm "
                "the cache via resolve_for_backtest / resolve_for_live."
            )
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
    # Live-trading resolve entrypoint (registry-backed)
    # ------------------------------------------------------------------

    async def resolve_for_live(self, symbols: list[str]) -> list[str]:
        """Return canonical Nautilus ``InstrumentId`` strings for ``symbols``.

        Warm path: registry hit by alias OR raw_symbol → return the active
        alias. Cold path: delegate to the Phase-1 closed-universe
        :func:`canonical_instrument_id` helper, resolve the built
        :class:`InstrumentSpec` through the existing cache-first
        :meth:`resolve`, then upsert
        ``instrument_definitions`` + ``instrument_aliases`` so the next
        call goes down the warm path.

        Non-hot-path; uses ``self._db`` + optional IB qualify round-trips.
        Callers must pre-warm before ``TradingNode.run()`` (gotchas #9,
        #11 — dynamic instrument loading on the trading critical path
        fails at the first bar event, not at startup).

        Cold-miss scope:
            The cold-miss path currently delegates to the closed-universe
            :func:`live_instrument_bootstrap.canonical_instrument_id`
            helper. Symbols outside ``{AAPL, MSFT, SPY, EUR/USD, ES}``
            will raise ``ValueError`` from that helper. To add a new
            symbol:

            1. Extend :func:`canonical_instrument_id`'s if-chain.
            2. Extend :meth:`_spec_from_canonical` (below) with the new
               venue case.
            3. Pre-warm the registry via ``msai instruments refresh
               --symbols <NEW>`` so future hits go down the warm path
               instead of the cold path.

        Raises:
            ValueError: A cold-miss path was required but ``self._qualifier``
                is ``None`` — construct the :class:`SecurityMaster` with
                ``qualifier=...`` for any live-trading entrypoint.
        """
        # Use CME-local date (America/Chicago), matching what
        # canonical_instrument_id + build_ib_instrument_provider_config
        # use — otherwise on late-UTC-night runs the UTC date disagrees
        # with the exchange date and the two resolve to different
        # quarterly contracts.
        from msai.services.nautilus.live_instrument_bootstrap import (
            canonical_instrument_id,
            exchange_local_today,
        )
        from msai.services.nautilus.security_master.registry import (
            InstrumentRegistry,
        )

        registry = InstrumentRegistry(self._db)
        today = exchange_local_today()
        out: list[str] = []
        for sym in symbols:
            # Warm path A — caller passed an already-qualified dotted alias.
            # Thread ``today`` (exchange-local CME date) so roll-sensitive
            # aliases are windowed against the same date the cold path +
            # canonical_instrument_id use — otherwise a late-UTC-night
            # run could resolve a different quarterly contract here than
            # elsewhere in the live path.
            if "." in sym:
                idef = await registry.find_by_alias(
                    sym, provider="interactive_brokers", as_of_date=today
                )
                if idef is not None:
                    out.append(sym)
                    continue
            # Warm path B — caller passed a bare ticker, resolve to
            # active alias. For roll-sensitive symbols (futures that
            # quarterly-roll like ES), the stored active alias may be
            # stale after an expiry; compare against today's canonical
            # and fall through to cold path if it differs so the
            # re-qualify + upsert closes the old alias and opens the
            # new one. For non-rollable symbols (AAPL/MSFT/SPY/EUR/USD)
            # the registry IS authoritative — a legitimate alias move
            # (e.g. AAPL.NASDAQ → AAPL.ARCA) must be honored, not
            # reverted to canonical_instrument_id's hardcoded default.
            idef = await registry.find_by_raw_symbol(sym, provider="interactive_brokers")
            if idef is not None:
                active_alias = next((a for a in idef.aliases if a.effective_to is None), None)
                if active_alias is not None:
                    is_stale = False
                    if sym in _ROLL_SENSITIVE_ROOTS:
                        try:
                            expected = canonical_instrument_id(sym, today=today)
                        except ValueError:
                            expected = None
                        if expected is not None and expected != active_alias.alias_string:
                            is_stale = True
                    if not is_stale:
                        out.append(active_alias.alias_string)
                        continue
            # Cold path — delegate to existing live_instrument_bootstrap
            # front-month rollover + existing SecurityMaster.resolve(spec).
            # Reason: live_instrument_bootstrap.canonical_instrument_id(...)
            # holds the closed-universe roll logic (ES → ESM6.CME at spawn
            # today); we reuse it rather than reinventing. The returned
            # canonical alias string is then used to build an InstrumentSpec
            # via _spec_from_canonical() and the spec is resolved through
            # the existing cache-first path (which triggers an IB qualify
            # round-trip on cache miss).
            if self._qualifier is None:
                raise ValueError(
                    f"Cold-miss resolve for {sym!r} requires an IBQualifier — "
                    "construct SecurityMaster with qualifier=... for live use."
                )
            canonical = canonical_instrument_id(sym, today=today)
            spec = self._spec_from_canonical(canonical, today=today)
            instrument = await self.resolve(spec)  # cache-first
            alias_str = str(instrument.id)
            routing_venue = instrument.id.venue.value
            listing_venue = routing_venue
            details = self._qualifier._provider.contract_details.get(instrument.id)
            if details is not None and getattr(details, "contract", None) is not None:
                primary = getattr(details.contract, "primaryExchange", None) or None
                if primary:
                    listing_venue = primary
            await self._upsert_definition_and_alias(
                raw_symbol=instrument.raw_symbol.value,
                listing_venue=listing_venue,
                routing_venue=routing_venue,
                asset_class=self._asset_class_for_instrument(instrument),
                alias_string=alias_str,
            )
            out.append(alias_str)
        return out

    # ------------------------------------------------------------------
    # Backtest resolve entrypoint (registry-backed)
    # ------------------------------------------------------------------

    async def resolve_for_backtest(
        self,
        symbols: list[str],
        *,
        start: str | None = None,
        end: str | None = None,
        dataset: str = "GLBX.MDP3",
    ) -> list[str]:
        """Return canonical Nautilus ``InstrumentId`` strings for ``symbols``.

        Four paths:

        1. ``<root>.Z.<N>`` continuous pattern → delegate to
           :meth:`_resolve_databento_continuous` which warm-hits
           by ``raw_symbol`` and falls through to the Databento
           definition fetch + synthesis on miss.
        2. Any other dotted input (e.g. ``"ESH4.CME"``) → warm-hit
           via :meth:`InstrumentRegistry.find_by_alias` under
           ``provider="databento"``. Returned alias IS the input string.
        3. Bare ticker (e.g. ``"AAPL"``) → warm-hit via
           :meth:`InstrumentRegistry.find_by_raw_symbol` under
           ``provider="databento"``, return its active alias string.
        4. Miss on the warm paths → raise :class:`DatabentoDefinitionMissing`
           with an actionable operator hint.

        Backtests are fail-loud on cold-miss: the operator must run
        ``msai instruments refresh`` first. The one exception is the
        ``.Z.N`` continuous path, which *does* synthesize on miss because
        the front-month roll is a known-good Databento-side operation
        with no IB round-trip.

        Args:
            symbols: Requested symbols. Accepts any of the three input
                shapes (continuous pattern / dotted alias / bare ticker).
            start: Definition window lower bound (``YYYY-MM-DD``). Only
                used on the ``.Z.N`` cold-miss path. Defaults to
                ``"2024-01-01"``.
            end: Definition window upper bound (``YYYY-MM-DD``). Defaults
                to today UTC.
            dataset: Databento dataset for the ``.Z.N`` cold-miss path.
                Defaults to ``"GLBX.MDP3"`` (CME futures).

        Raises:
            DatabentoDefinitionMissing: A warm-path miss for a non-``.Z.N``
                symbol — operator has not pre-warmed the registry.
            ValueError: A ``.Z.N`` cold-miss but ``self._databento`` is
                ``None`` — cannot synthesize without the Databento client.
        """
        from msai.services.nautilus.security_master.registry import (
            InstrumentRegistry,
        )

        registry = InstrumentRegistry(self._db)
        # Window alias lookups by ``start`` so historical backtests get the
        # alias that was active *during* the backtest window, not today's
        # front-month / current listing venue. Falls back to today when the
        # caller doesn't scope a window (live-like resolve).
        as_of = date.fromisoformat(start) if start else datetime.now(UTC).date()
        out: list[str] = []
        for sym in symbols:
            # Path 1 — Databento continuous pattern.
            if is_databento_continuous_pattern(sym):
                out.append(
                    await self._resolve_databento_continuous(
                        sym, start=start, end=end, dataset=dataset
                    )
                )
                continue

            # Path 2 — dotted alias already in registry.
            if "." in sym:
                idef = await registry.find_by_alias(sym, provider="databento", as_of_date=as_of)
                if idef is not None:
                    out.append(sym)
                    continue
                raise DatabentoDefinitionMissing(
                    f"No registry row for alias {sym!r} under provider "
                    "'databento' — run `msai instruments refresh --symbols "
                    f"{sym}` to pre-warm the registry before the backtest."
                )

            # Path 3 — bare ticker, warm-hit by raw_symbol.
            idef = await registry.find_by_raw_symbol(sym, provider="databento")
            if idef is not None:
                active_alias = next(
                    (
                        a
                        for a in idef.aliases
                        if a.effective_from <= as_of
                        and (a.effective_to is None or a.effective_to > as_of)
                    ),
                    None,
                )
                if active_alias is not None:
                    out.append(active_alias.alias_string)
                    continue

            # Path 4 — cold-miss, fail loud.
            raise DatabentoDefinitionMissing(
                f"No registry row for raw_symbol {sym!r} under provider "
                "'databento' — run `msai instruments refresh --symbols "
                f"{sym}` to pre-warm the registry before the backtest."
            )
        return out

    async def _resolve_databento_continuous(
        self,
        sym: str,
        *,
        start: str | None,
        end: str | None,
        dataset: str,
    ) -> str:
        """Resolve a ``<root>.Z.<N>`` continuous pattern for backtest.

        Step 1 — Warm path: :meth:`InstrumentRegistry.find_by_raw_symbol`
        under ``provider="databento"``. If an active alias exists for the
        raw continuous symbol, return it without touching Databento.

        Step 2 — Cold path: download the Databento ``definition``
        payload for the window ``[start, end)`` via
        :meth:`DatabentoClient.fetch_definition_instruments`, synthesize
        a :class:`ResolvedInstrumentDefinition` via
        :func:`resolved_databento_definition`, and upsert the registry
        through the shared idempotent helper
        :meth:`_upsert_definition_and_alias` (with
        ``provider="databento"`` + ``venue_format="databento_continuous"``
        — matches the CHECK constraints on both tables).

        Idempotency: the ``_upsert_definition_and_alias`` helper is scoped
        on ``(raw_symbol, provider, asset_class)`` for the definition
        and ``(alias_string, provider)`` for the active alias, so a
        second call with the same window refreshes timestamps without
        raising :class:`IntegrityError`.

        Raises:
            ValueError: ``self._databento`` is ``None`` on cold-miss —
                cannot fetch the definition payload.
        """
        from msai.services.nautilus.security_master.registry import (
            InstrumentRegistry,
        )

        raw = raw_symbol_from_request(sym)
        registry = InstrumentRegistry(self._db)

        # Step 1 — warm path.
        idef = await registry.find_by_raw_symbol(raw, provider="databento")
        if idef is not None:
            active_alias = next((a for a in idef.aliases if a.effective_to is None), None)
            if active_alias is not None:
                return active_alias.alias_string

        # Step 2 — cold path. Databento client is required.
        if self._databento is None:
            raise ValueError(
                f"DatabentoClient required to synthesize continuous {sym!r} "
                "on cold-miss — construct SecurityMaster with "
                "databento_client=... or pre-warm the registry via "
                "`msai instruments refresh`."
            )

        resolved_start = start or "2024-01-01"
        resolved_end = end or datetime.now(UTC).date().isoformat()
        definition_path = (
            settings.databento_definition_root
            / dataset
            / raw
            / f"{resolved_start}_{resolved_end}.definition.dbn.zst"
        )
        instruments = await self._databento.fetch_definition_instruments(
            raw,
            resolved_start,
            resolved_end,
            dataset=dataset,
            target_path=definition_path,
        )
        resolved = resolved_databento_definition(
            raw_symbol=raw,
            instruments=instruments,
            dataset=dataset,
            start=resolved_start,
            end=resolved_end,
            definition_path=definition_path,
        )
        await self._upsert_definition_and_alias(
            raw_symbol=resolved.raw_symbol,
            listing_venue=resolved.listing_venue,
            routing_venue=resolved.routing_venue,
            asset_class=resolved.asset_class,
            alias_string=resolved.instrument_id,
            provider="databento",
            venue_format="databento_continuous",
        )
        return resolved.instrument_id

    @staticmethod
    def _asset_class_for_instrument(instrument: Any) -> str:
        """Derive the registry's ``asset_class`` column value from a Nautilus
        :class:`Instrument` via its runtime class name.

        Delegates to
        :func:`security_master.continuous_futures.asset_class_for_instrument_type`
        so both the live-resolve path (this method, takes Instrument) and
        the Databento backtest-resolve path (takes a string type name from
        the serialized payload) share one mapping and cannot drift.

        Note that this differs from :class:`InstrumentSpec.asset_class`
        which uses ``'future'`` (singular) as its literal — the spec
        enum is a separate taxonomy for *input*, not the registry's
        storage enum.
        """
        from msai.services.nautilus.security_master.continuous_futures import (
            asset_class_for_instrument_type,
        )

        return asset_class_for_instrument_type(instrument.__class__.__name__)

    def asset_class_for_alias(self, alias_str: str) -> str | None:
        """Canonical alias → ingest-taxonomy asset_class.

        Public wrapper over :meth:`_spec_from_canonical` that translates
        the registry/spec taxonomy (``"equity"`` / ``"future"`` /
        ``"option"`` / ``"forex"`` / ``"index"``) to the ingest /
        Parquet-storage taxonomy (``"stocks"`` / ``"futures"`` /
        ``"options"`` / ``"forex"`` / ``"crypto"``).

        This mapping is critical — if the wrong name reaches
        ``DataIngestionService._resolve_plan`` the Parquet writes go
        to the wrong directory (e.g. ``data/parquet/equity/``) while
        the catalog reader expects ``data/parquet/stocks/``, producing
        a perpetual auto-heal loop.

        Returns ``None`` if the alias shape is unknown — callers fall
        back to the shape heuristic in
        :func:`msai.services.backtests.derive_asset_class.derive_asset_class_sync`.
        """
        if not alias_str:
            return None
        try:
            spec = self._spec_from_canonical(alias_str)
        except Exception:  # noqa: BLE001 — unknown venue / malformed alias
            log.warning(
                "asset_class_for_alias_spec_failed",
                alias=alias_str,
                exc_info=True,
            )
            return None

        registry_taxon: str | None = getattr(spec, "asset_class", None)
        if registry_taxon is None:
            return None
        # Unknown taxonomy passes through unchanged — operator can still
        # see it and decide; tests parametrize each known key.
        return _REGISTRY_TO_INGEST_ASSET_CLASS.get(registry_taxon, registry_taxon)

    def _spec_from_canonical(
        self,
        canonical: str,
        *,
        today: date | None = None,
    ) -> InstrumentSpec:
        """Parse an already-resolved canonical alias string into an
        :class:`InstrumentSpec` for downstream :meth:`resolve`.

        Reuses the venue mapping established by
        :func:`live_instrument_bootstrap.canonical_instrument_id`. Closed
        universe:

        - ``AAPL.NASDAQ`` / ``MSFT.NASDAQ`` → equity / NASDAQ
        - ``SPY.ARCA`` → equity / ARCA
        - ``EUR/USD.IDEALPRO`` → forex / IDEALPRO
        - ``ESM6.CME`` (or similar) → future / CME.
          ``today`` is used to compute the third-Friday expiry. Without
          it the spec has ``expiry=None`` and ``IBQualifier`` maps it
          to ``CONTFUT`` — IB Gateway then returns the continuous
          placeholder, not the concrete front-month.

        Raises:
            ValueError: On an unknown venue suffix — callers should
                widen the closed universe by adding a case here first.
        """
        symbol, _, venue = canonical.rpartition(".")
        if not venue:
            raise ValueError(f"Canonical alias {canonical!r} has no venue suffix")
        if venue == "NASDAQ":
            return InstrumentSpec(asset_class="equity", symbol=symbol, venue="NASDAQ")
        if venue == "ARCA":
            return InstrumentSpec(asset_class="equity", symbol=symbol, venue="ARCA")
        if venue == "IDEALPRO":
            # symbol here is "EUR/USD"; base = "EUR", quote = "USD"
            base, _, quote = symbol.partition("/")
            return InstrumentSpec(
                asset_class="forex",
                symbol=base,
                venue="IDEALPRO",
                currency=quote or "USD",
            )
        if venue == "CME":
            # Import locally to avoid a security_master →
            # live_instrument_bootstrap cycle at module import time.
            from msai.services.nautilus.live_instrument_bootstrap import (
                _current_quarterly_expiry,
                exchange_local_today,
                third_friday_of,
            )

            if today is None:
                today = exchange_local_today()
            # Incoming symbol is the local-symbol form ("ESM6") — root
            # + 1-char month code + 1-digit year. InstrumentSpec
            # RECOMPUTES that suffix from expiry, so passing "ESM6" +
            # expiry would yield "ESM6M6.CME". Strip to the root.
            root = symbol[:-2]
            expiry_str = _current_quarterly_expiry(today)  # YYYYMM
            expiry = third_friday_of(
                int(expiry_str[0:4]),
                int(expiry_str[4:6]),
            )
            return InstrumentSpec(
                asset_class="future",
                symbol=root,
                venue="CME",
                expiry=expiry,
            )
        raise ValueError(
            f"Unknown venue {venue!r} in canonical {canonical!r} — extend "
            "SecurityMaster._spec_from_canonical for new venues."
        )

    async def _upsert_definition_and_alias(
        self,
        *,
        raw_symbol: str,
        listing_venue: str,
        routing_venue: str,
        asset_class: str,
        alias_string: str,
        provider: str = "interactive_brokers",
        venue_format: str = "exchange_name",
        source_venue_raw: str | None = None,
    ) -> None:
        """Idempotent upsert: one :class:`InstrumentDefinition` row +
        one active :class:`InstrumentAlias` row.

        Called from both :meth:`resolve_for_live` (provider defaults to
        ``interactive_brokers``, venue_format ``exchange_name``) and
        :meth:`_resolve_databento_continuous` (provider ``databento``,
        venue_format ``databento_continuous``).

        Idempotency: scoped to ``(raw_symbol, provider, asset_class)`` —
        matches the ``uq_instrument_definitions_symbol_provider_asset``
        unique constraint created by the registry migration. A second call
        with the same tuple refreshes ``refreshed_at`` and is a no-op on
        the alias side (same-day ``(alias_string, provider,
        effective_from)`` matches the
        ``uq_instrument_aliases_string_provider_from`` unique constraint,
        which ``ON CONFLICT DO NOTHING`` quietly handles).

        Race-safety: both statements use PostgreSQL ``INSERT ... ON
        CONFLICT`` so concurrent resolvers for the same symbol can't
        collide on the unique constraints. Mirrors the pattern in
        :meth:`_write_cache`.
        """
        from sqlalchemy import text

        from msai.models.instrument_alias import InstrumentAlias
        from msai.models.instrument_definition import InstrumentDefinition
        from msai.services.nautilus.security_master.venue_normalization import (
            normalize_alias_for_registry,
        )

        # FX raw_symbol invariant: registry stores BASE/QUOTE slash form.
        # IB's localSymbol for CASH pairs is dot form ("EUR.USD"); the
        # live-resolver's _build_contract_spec splits on "/", and warm
        # lookups use the operator-typed "EUR/USD". Normalize at the
        # storage boundary so neither side drifts.
        if asset_class == "fx" and "/" not in raw_symbol and raw_symbol.count(".") == 1:
            raw_symbol = raw_symbol.replace(".", "/")

        # Preserve raw Databento venue as provenance when normalizing MIC
        # aliases. If caller omits source_venue_raw and the alias is in MIC
        # form (venue_format="mic_code"), auto-derive from the PRE-normalization
        # alias_string. Continuous-futures aliases (venue_format="databento_continuous")
        # are already in exchange-name form and don't need provenance capture.
        if (
            provider == "databento"
            and venue_format == "mic_code"
            and source_venue_raw is None
            and "." in alias_string
        ):
            source_venue_raw = alias_string.rsplit(".", 1)[1]

        # Normalize Databento MIC → exchange-name at the write boundary so
        # the registry has ONE canonical alias convention. Only applies when
        # the caller explicitly flagged the alias as MIC form. IB aliases
        # (exchange_name) and continuous-futures (databento_continuous) pass
        # through unchanged. Unknown MICs raise UnknownDatabentoVenueError
        # (bootstrap service surfaces as outcome=unmapped_venue).
        if venue_format == "mic_code":
            alias_string = normalize_alias_for_registry(provider, alias_string)

        # Advisory lock serializes concurrent upserts on the same
        # (provider, raw_symbol, asset_class) across workers/processes.
        # Reentrant for the same session, so orchestrators that pre-acquire
        # the same key (bootstrap service) re-acquire here as a no-op.
        await self._db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": compute_advisory_lock_key(provider, raw_symbol, asset_class)},
        )

        # Divergence detection. Fires only on an IB venue TRANSITION that
        # disagrees with the prior Databento-authored venue — i.e. this
        # refresh actually changed IB's active alias AND the new IB venue
        # differs from the Databento-authored one. Without the IB-side
        # transition check the counter would re-fire on every idempotent
        # refresh after a real migration (e.g. Databento=ARCA, IB=BATS
        # repeated refresh keeps incrementing the metric), inflating
        # counts and obscuring real migration events.
        #
        # Runs BEFORE the UPSERT so the reads see pre-mutation state;
        # alias normalization ensures the Databento comparison only
        # fires on REAL migrations, not notation-only MIC-vs-exchange-name
        # differences.
        if provider == "interactive_brokers":
            from msai.services.observability.trading_metrics import (
                REGISTRY_VENUE_DIVERGENCE_TOTAL,
            )

            prior_rows = await self._db.execute(
                select(InstrumentAlias.alias_string, InstrumentAlias.provider)
                .join(
                    InstrumentDefinition,
                    InstrumentDefinition.instrument_uid == InstrumentAlias.instrument_uid,
                )
                .where(
                    InstrumentDefinition.raw_symbol == raw_symbol,
                    InstrumentDefinition.asset_class == asset_class,
                    InstrumentAlias.provider.in_(("databento", "interactive_brokers")),
                    InstrumentAlias.effective_to.is_(None),
                )
            )
            prior_databento_alias: str | None = None
            prior_ib_alias: str | None = None
            for alias_row, provider_row in prior_rows.all():
                if provider_row == "databento":
                    prior_databento_alias = alias_row
                elif provider_row == "interactive_brokers":
                    prior_ib_alias = alias_row

            if (
                prior_databento_alias is not None
                and "." in prior_databento_alias
                and "." in alias_string
            ):
                prior_databento_venue = prior_databento_alias.rsplit(".", 1)[1]
                new_ib_venue = alias_string.rsplit(".", 1)[1]
                prior_ib_venue = (
                    prior_ib_alias.rsplit(".", 1)[1]
                    if prior_ib_alias is not None and "." in prior_ib_alias
                    else None
                )
                # Only fire when (a) the new IB venue disagrees with the
                # Databento authority AND (b) this refresh actually changed
                # IB's own alias — a repeated no-op refresh (prior_ib_venue
                # == new_ib_venue) must NOT re-increment.
                is_migration = prior_databento_venue != new_ib_venue
                ib_transitioned = prior_ib_venue != new_ib_venue
                if is_migration and ib_transitioned:
                    REGISTRY_VENUE_DIVERGENCE_TOTAL.labels(
                        databento_venue=prior_databento_venue,
                        ib_venue=new_ib_venue,
                    ).inc()
                    log.warning(
                        "registry_bootstrap_divergence",
                        raw_symbol=raw_symbol,
                        asset_class=asset_class,
                        previous_provider="databento",
                        previous_venue=prior_databento_venue,
                        new_provider="interactive_brokers",
                        new_venue=new_ib_venue,
                        prior_ib_venue=prior_ib_venue,
                    )

        now = datetime.now(UTC)
        def_stmt = (
            pg_insert(InstrumentDefinition)
            .values(
                raw_symbol=raw_symbol,
                listing_venue=listing_venue,
                routing_venue=routing_venue,
                asset_class=asset_class,
                provider=provider,
                lifecycle_state="active",
                refreshed_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_instrument_definitions_symbol_provider_asset",
                # Refresh venue fields on conflict so an alias move
                # (e.g. AAPL.NASDAQ → AAPL.ARCA) propagates to the
                # definition row, not just the alias table. Without
                # this, callers reading InstrumentDefinition get
                # permanently stale venue metadata after the first
                # venue change.
                set_={
                    "refreshed_at": now,
                    "listing_venue": listing_venue,
                    "routing_venue": routing_venue,
                },
            )
            .returning(InstrumentDefinition.__table__.c.instrument_uid)
        )
        result = await self._db.execute(def_stmt)
        instrument_uid = result.scalar_one()

        today = now.date()

        # Close any previous active aliases for this
        # ``(instrument_uid, provider)`` so the new alias becomes the single
        # active one per the half-open ``[effective_from, effective_to)``
        # window invariant. Callers pick the active alias via
        # ``next((a for a in idef.aliases if a.effective_to is None))`` —
        # without this, a futures roll or repeated refreshes on different
        # days leave multiple aliases active simultaneously and the caller
        # picks arbitrarily.
        close_stmt = (
            update(InstrumentAlias)
            .where(
                InstrumentAlias.instrument_uid == instrument_uid,
                InstrumentAlias.provider == provider,
                InstrumentAlias.effective_to.is_(None),
                # Don't close an alias that's about to be re-inserted today
                # (idempotent same-day refresh path — the insert below is
                # ON CONFLICT DO NOTHING on
                # ``(alias_string, provider, effective_from)``).
                InstrumentAlias.alias_string != alias_string,
            )
            .values(effective_to=today)
        )
        await self._db.execute(close_stmt)

        # Alias upsert — ON CONFLICT DO NOTHING since the uniqueness key
        # includes ``effective_from`` so a same-day re-upsert is a no-op.
        alias_stmt = (
            pg_insert(InstrumentAlias)
            .values(
                instrument_uid=instrument_uid,
                alias_string=alias_string,
                venue_format=venue_format,
                provider=provider,
                effective_from=today,
                source_venue_raw=source_venue_raw,
            )
            .on_conflict_do_nothing(
                constraint="uq_instrument_aliases_string_provider_from",
            )
        )
        await self._db.execute(alias_stmt)
        await self._db.flush()

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
        trading_hours_json: dict[str, Any] | None,
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

        stmt = pg_insert(InstrumentCache).values(
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

    def _trading_hours_for(self, *, canonical_id: str) -> dict[str, Any] | None:
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


def _ib_contract_to_dict(contract) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Serialize an IBContract msgspec struct to a plain dict.

    ``msgspec.structs.asdict`` is the canonical way to convert a
    frozen struct into a dict without copying Nautilus's own
    bespoke serialization. We keep this as a small helper so the
    service doesn't take a direct dependency on msgspec.
    """
    import msgspec

    return msgspec.structs.asdict(contract)
