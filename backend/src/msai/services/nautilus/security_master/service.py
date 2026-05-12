"""SecurityMaster service.

Registry-backed resolver answering "give me the Nautilus
``Instrument`` for this logical spec" with the fewest possible IB
round-trips:

1. Compute the canonical ID from the spec
   (``InstrumentSpec.canonical_id()``).
2. Look up ``instrument_aliases`` for an active row at the
   exchange-local date.
3. Warm hit (equity/fx) → reconstruct the Nautilus ``Instrument``
   directly from the spec via
   :meth:`SecurityMaster._build_instrument_from_spec`.
4. Warm miss OR warm-hit on future/option/index → qualify via
   :class:`IBQualifier` (which delegates to Nautilus's
   ``InteractiveBrokersInstrumentProvider``), extract trading hours
   from the qualifier provider's ``contract_details``, upsert the
   registry row, and return the qualified instrument.

Why this is separate from :class:`IBQualifier`:

- The qualifier owns the IB round-trip mechanics (contract
  construction, provider delegation, batching).
- The service owns the registry control-plane + the hot-path
  routing.

Bulk resolve semantics:

- :meth:`bulk_resolve` issues ONE SELECT to find every warm-hit
  alias, then per-spec qualification on the residual misses — so
  a batch of 100 specs with 95 warm hits fires exactly 5 IB
  requests (not 100). IB's ``reqContractDetails`` rate limit is
  50 msg/sec and each live deployment pre-loads every instrument
  its strategies need.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — used in dataclass field annotation evaluated at runtime
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.services.nautilus.security_master.continuous_futures import (
    is_databento_continuous_pattern,
    raw_symbol_from_request,
    resolved_databento_definition,
)
from msai.services.nautilus.security_master.parser import (
    extract_trading_hours,
)
from msai.services.nautilus.security_master.types import (
    REGISTRY_TO_INGEST_ASSET_CLASS as _REGISTRY_TO_INGEST_ASSET_CLASS,
)
from msai.services.nautilus.security_master.types import (
    RegistryAssetClass as _RegistryAssetClass,
)

_REGISTRY_TO_SPEC_ASSET_CLASS: dict[_RegistryAssetClass, str] = {
    "equity": "equity",
    "futures": "future",  # registry plural → spec singular (specs.AssetClass)
    "fx": "forex",
    "option": "option",
    "crypto": "crypto",
}
"""Bridge from registry asset_class taxonomy
(:data:`RegistryAssetClass` — the values stored in
``instrument_definitions.asset_class``) to the spec taxonomy
(:class:`InstrumentSpec.asset_class`, which uses ``future``/``forex``).
Used by :meth:`SecurityMaster._resolve_one` so the warm-hit dispatch
into :meth:`_build_instrument_from_spec` doesn't reach across two
implicit-aligned vocabularies."""

if TYPE_CHECKING:
    from nautilus_trader.model.instruments import Instrument
    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.models.instrument_definition import InstrumentDefinition
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.nautilus.security_master.ib_qualifier import IBQualifier
    from msai.services.nautilus.security_master.specs import InstrumentSpec
    from msai.services.nautilus.security_master.types import (
        IngestAssetClass,
        Provider,
        RegistryAssetClass,
        VenueFormat,
    )


log = get_logger(__name__)


def compute_advisory_lock_key(
    provider: Provider, raw_symbol: str, asset_class: RegistryAssetClass
) -> int:
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


def compute_blake2b_digest_key(*parts: str) -> int:
    """blake2b digest of arbitrary string parts, rendered as a positive int.

    Shared primitive with :func:`compute_advisory_lock_key` but accepts any
    number of string parts (joined with a null separator). Used by callers
    that need a deterministic fingerprint of a composite key (e.g. the
    symbol-onboarding ``job_id_digest``).
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.blake2b("\x00".join(parts).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False) & 0x7FFFFFFFFFFFFFFF


class DatabentoDefinitionMissing(Exception):  # noqa: N818 — spec-mandated name (codex parity)
    """Raised by :meth:`SecurityMaster.resolve_for_backtest` when a requested
    symbol has no active registry row under the ``databento`` provider and
    the operator has not pre-warmed the registry.

    Backtests are fail-loud on cold-miss — the error carries the original
    symbol and a provider-specific operator hint:

    * Databento equity/ETF symbols → ``msai instruments bootstrap
      --provider databento --symbols X``.
    * Databento ``.Z.N`` continuous-futures cold-miss synthesis →
      ``msai instruments refresh`` (the futures-aware path; see
      :meth:`SecurityMaster._resolve_databento_continuous`).

    Hint dispatch lives in ``resolve_for_backtest`` itself; this class
    docstring records the contract so a future reader can audit it.
    """


class DatabentoClientUnavailableError(LookupError):
    """Raised when :meth:`SecurityMaster._resolve_databento_continuous`
    needs to synthesize a continuous-futures alias on cold-miss but the
    :class:`SecurityMaster` was constructed without a ``DatabentoClient``.

    Subclasses :class:`LookupError` for symmetry with the IB-side
    :class:`IBContractNotFoundError` — both signal "the upstream provider
    cannot resolve this symbol right now".
    """


@dataclass(frozen=True, slots=True)
class AliasResolution:
    """Aggregate readiness view for one (symbol, asset_class) lookup.

    Built from the union of all active alias rows for a single
    ``instrument_uid``. Distinct from :meth:`SecurityMaster.resolve_for_backtest`
    and :meth:`SecurityMaster.lookup_for_live`, which return scalar alias
    strings or live contract specs respectively — this aggregator powers
    the three-state readiness contract (``registered`` /
    ``backtest_data_available`` / ``live_qualified``) surfaced by the
    Symbol Onboarding readiness endpoint.
    """

    instrument_uid: uuid.UUID | None
    primary_provider: str  # e.g. "databento"; empty string when unregistered
    has_ib_alias: bool
    registry_asset_class: RegistryAssetClass  # registry-taxonomy value from caller

    def coverage_summary_hint(self) -> str | None:
        if self.instrument_uid is None:
            return None
        return f"Registered via {self.primary_provider}; live-qualified: {self.has_ib_alias}"


@dataclass(frozen=True, slots=True)
class _RegisteredInstrument:
    """One registered instrument with its aggregated live-qualification flag.

    Returned by :meth:`SecurityMaster.list_registered_instruments` for the
    bulk inventory endpoint. ``live_qualified`` is computed as
    "any active alias for this instrument_uid carries
    ``provider='interactive_brokers'``" so a single SELECT serves all
    inventory rows.
    """

    instrument_uid: uuid.UUID
    raw_symbol: str  # matches InstrumentDefinition.raw_symbol
    asset_class: str
    provider: str
    live_qualified: bool
    last_refresh_at: datetime | None


class SecurityMaster:
    """Registry-backed instrument resolver.

    Args:
        qualifier: IB qualifier adapter that hits Nautilus's
            :class:`InteractiveBrokersInstrumentProvider` for registry
            misses (and for warm-hit non-equity/fx asset classes that
            need a fresh Nautilus :class:`Instrument` from live contract
            details). Production wires a short-lived provider bound to
            an isolated IB client; tests pass a stub.
        db: Async session — registry reads/writes share this session
            with the caller's transaction.
        databento_client: Used by the continuous-futures backtest path
            (:meth:`_resolve_databento_continuous`). ``None`` is
            permitted for live-only callers; a cold-miss on a Databento
            continuous symbol with ``databento_client=None`` will raise.
    """

    def __init__(
        self,
        *,
        qualifier: IBQualifier | None = None,
        db: AsyncSession,
        databento_client: DatabentoClient | None = None,
    ) -> None:
        self._qualifier = qualifier
        self._db = db
        # Used by the continuous-futures backtest path
        # (``_resolve_databento_continuous``). ``None`` is permitted for
        # live-only callers — a cold-miss on a Databento continuous symbol
        # with ``self._databento is None`` will raise.
        self._databento = databento_client

    async def resolve(self, spec: InstrumentSpec) -> Instrument:
        """Resolve a single spec via the registry.

        Warm path (equity / fx): registry has an active alias for the
        spec's canonical_id → reconstruct the Nautilus
        :class:`Instrument` from the spec via
        :meth:`_build_instrument_from_spec`.

        Warm hit on future / option / index OR registry miss: qualify
        via the IB qualifier, upsert the definition + alias row (with
        extracted trading_hours), return the qualified instrument.
        ``listing_venue`` is derived via
        :meth:`IBQualifier.listing_venue_for`; ``routing_venue`` is the
        Nautilus-resolved venue (e.g. ``SMART`` for stocks).

        Raises:
            ValueError: a path that requires the qualifier was reached
                with ``self._qualifier is None`` — either a cold miss
                on any asset class, or a warm hit on an asset class the
                spec-builder doesn't support in v1.
        """
        from msai.services.nautilus.live_instrument_bootstrap import (  # noqa: PLC0415
            exchange_local_today,
        )
        from msai.services.nautilus.security_master.registry import (  # noqa: PLC0415
            InstrumentRegistry,
        )

        registry = InstrumentRegistry(self._db)
        # Use exchange-local (America/Chicago) date for IB alias windowing;
        # date.today() (UTC) would disagree with the supervisor's spawn-time
        # lookup_for_live(as_of_date=spawn_today) and could resolve a
        # different futures contract on roll-day.
        today = exchange_local_today()

        idef = await registry.find_by_alias(
            spec.canonical_id(),
            provider="interactive_brokers",
            as_of_date=today,
        )
        return await self._resolve_one(spec, warm_def=idef)

    async def bulk_resolve(self, specs: list[InstrumentSpec]) -> list[Instrument]:
        """Bulk resolve via the registry — one SELECT for all warm hits,
        then per-spec qualification on the residual cold-misses (and
        warm-hit non-equity/fx asset classes that need IB qualification).
        """
        if not specs:
            return []

        from msai.services.nautilus.live_instrument_bootstrap import (  # noqa: PLC0415
            exchange_local_today,
        )
        from msai.services.nautilus.security_master.registry import (  # noqa: PLC0415
            InstrumentRegistry,
        )

        registry = InstrumentRegistry(self._db)
        # Compute ``today`` once for the whole batch so per-spec resolution
        # cannot drift across a roll-day midnight boundary mid-call.
        today = exchange_local_today()
        canonical_ids = [spec.canonical_id() for spec in specs]
        warm_aliases = await registry.find_by_aliases_bulk(
            canonical_ids, provider="interactive_brokers", as_of_date=today
        )

        results: list[Instrument] = []
        for spec, canonical_id in zip(specs, canonical_ids, strict=True):
            results.append(
                await self._resolve_one(
                    spec,
                    warm_def=warm_aliases.get(canonical_id),
                )
            )
        return results

    async def _resolve_one(
        self,
        spec: InstrumentSpec,
        *,
        warm_def: InstrumentDefinition | None,
    ) -> Instrument:
        """Internal resolver shared by :meth:`resolve` and :meth:`bulk_resolve`.

        Takes an already-fetched warm :class:`InstrumentDefinition`
        (``None`` for cold miss). Callers compute ``today`` themselves
        before fetching ``warm_def`` so that the bulk path's roll-day
        window stays consistent across every spec; this method does not
        need it.

        Spec-build is scoped to ``equity`` + ``fx`` per
        :meth:`_build_instrument_from_spec`. Future / option / index warm
        hits fall through to the qualifier (the IB provider IS the source
        of truth for those at runtime — no Postgres payload blob to
        replay), matching the cold-miss path's behavior. Idempotent
        registry upsert at the end of qualification is a no-op when the
        warm row already exists.
        """
        # Bridge the warm row's registry-taxonomy ``asset_class`` (e.g.
        # ``futures``/``fx``) to the spec-taxonomy value
        # :meth:`_build_instrument_from_spec` dispatches on (``future``/
        # ``forex``). Without this bridge the dispatch worked by accident
        # because callers always built specs with spec values; the bridge
        # makes the cross-walk explicit and mypy-typed.
        spec_asset_class = (
            _REGISTRY_TO_SPEC_ASSET_CLASS.get(warm_def.asset_class)  # type: ignore[call-overload]
            if warm_def is not None
            else None
        )
        if warm_def is not None and spec_asset_class in {"equity", "forex"}:
            return self._build_instrument_from_spec(spec)

        # Either: (a) cold miss, OR (b) warm hit on future/option/index
        # which spec-build can't satisfy in v1. Both paths require an
        # IB qualifier.
        if self._qualifier is None:
            if warm_def is not None:
                raise ValueError(
                    f"Registry warm hit for asset_class={warm_def.asset_class!r} "
                    "requires an IBQualifier — equity/fx build from spec, "
                    "future/option/index require live IB qualification. "
                    "Construct SecurityMaster with qualifier=... or use "
                    "`live_resolver.lookup_for_live` directly for non-IB callers."
                )
            raise ValueError(
                f"Registry miss for spec {spec!r} requires an IBQualifier — "
                "construct SecurityMaster with qualifier=... or pre-warm the "
                "registry via `msai instruments refresh`."
            )

        instrument = await self._qualifier.qualify(spec)
        canonical_id = spec.canonical_id()
        trading_hours_json = self._trading_hours_for(canonical_id=canonical_id)

        routing_venue = instrument.id.venue.value
        listing_venue = self._qualifier.listing_venue_for(instrument)

        await self._upsert_definition_and_alias(
            raw_symbol=instrument.raw_symbol.value,
            listing_venue=listing_venue,
            routing_venue=routing_venue,
            asset_class=self._asset_class_for_instrument(instrument),
            alias_string=str(instrument.id),
            trading_hours=trading_hours_json,
        )
        return instrument

    def _build_instrument_from_spec(self, spec: InstrumentSpec) -> Instrument:
        """Construct a Nautilus :class:`Instrument` from the spec WITHOUT
        consulting a Postgres payload blob.

        Scoped to ``equity`` and ``forex`` for v1. Live preload at
        :class:`InteractiveBrokersInstrumentProviderConfig(load_contracts=...)`
        in ``live_node_config.py`` is the production hydration path for
        futures + options at runtime (Nautilus's IB provider builds the
        Instrument from the qualified contract). Callers that need a
        Nautilus :class:`Instrument` for a future/option/index without a
        live IB connection should use ``live_resolver.lookup_for_live``
        — that's the canonical primitive post-PR-#37.

        Raises :class:`NotImplementedError` for unsupported asset classes
        with an operator-action hint.
        """
        from nautilus_trader.model.identifiers import Venue  # noqa: PLC0415
        from nautilus_trader.test_kit.providers import (  # noqa: PLC0415
            TestInstrumentProvider,
        )

        if spec.asset_class == "equity":
            return TestInstrumentProvider.equity(symbol=spec.symbol, venue=spec.venue)
        if spec.asset_class == "forex":
            # Nautilus default_fx_ccy expects "BASE/QUOTE" form; spec.symbol is
            # the base, spec.currency is the quote. The Nautilus API takes a
            # Venue object (not a str), so wrap the spec's venue suffix.
            pair = f"{spec.symbol}/{spec.currency}"
            return TestInstrumentProvider.default_fx_ccy(symbol=pair, venue=Venue(spec.venue))
        raise NotImplementedError(
            f"_build_instrument_from_spec does not support asset_class="
            f"{spec.asset_class!r} in v1 (only equity + forex). For futures, "
            f"options, and indexes, use `live_resolver.lookup_for_live` "
            f"directly — it returns a ResolvedInstrument from the registry "
            f"that the live preload can hydrate via "
            f"InteractiveBrokersInstrumentProviderConfig(load_contracts=...)."
        )

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
        2. Any other dotted input (e.g. ``"AAPL.XNAS"`` or
           ``"AAPL.NASDAQ"``) → warm-hit via
           :meth:`InstrumentRegistry.find_by_alias` under
           ``provider="databento"``. The user input is normalized to the
           registry's canonical exchange-name form via
           :func:`venue_normalization.normalize_databento_alias_for_lookup`
           (accepts both Databento MIC and exchange-name suffixes), so a
           Databento-bootstrapped row stored as ``AAPL.NASDAQ`` is hit by
           both ``AAPL.XNAS`` (MIC, what ``msai ingest stocks`` prints)
           and ``AAPL.NASDAQ`` (what ``lookup_for_live`` documents). The
           **returned** alias is always the canonical form, not the raw
           input — downstream code paths can compare against
           ``find_by_alias`` results without re-normalizing.
        3. Bare ticker (e.g. ``"AAPL"``) → warm-hit via
           :meth:`InstrumentRegistry.find_by_raw_symbol` under
           ``provider="databento"``, return its active alias string.
        4. Miss on the warm paths → raise :class:`DatabentoDefinitionMissing`
           with a provider-specific operator hint.

        Backtests are fail-loud on cold-miss. The error hints at the
        correct CLI subcommand for the symbol class:

        * Databento equity/ETF cold-miss → ``msai instruments bootstrap
          --provider databento --symbols X``.
        * ``.Z.N`` continuous-futures cold-miss is the one *self-healing*
          path — it synthesizes via Databento ``definition`` fetch without
          requiring a pre-warm step. No operator action needed.

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
                # Normalize user input at the read boundary: ``msai ingest
                # stocks`` prints Databento MIC aliases (``AAPL.XNAS``) but
                # the registry stores exchange-name aliases (``AAPL.NASDAQ``)
                # because the IB live-resolver exact-matches on the latter.
                # ``normalize_databento_alias_for_lookup`` is idempotent on
                # already-canonical input, so a caller passing ``AAPL.NASDAQ``
                # still resolves; unknown suffixes fail loud rather than
                # silently miss. See ``venue_normalization.py`` and Codex's
                # revised Item 4 in the 2026-05-12 fresh-VM-data-path-closure
                # PR briefing.
                from msai.services.nautilus.security_master.venue_normalization import (
                    UnknownDatabentoVenueError,
                    normalize_databento_alias_for_lookup,
                )

                try:
                    lookup_alias = normalize_databento_alias_for_lookup(sym)
                except UnknownDatabentoVenueError as exc:
                    raise DatabentoDefinitionMissing(
                        f"Cannot resolve alias {sym!r} for Databento backtest: "
                        f"{exc}. To register a Databento equity/ETF symbol, run "
                        f"`msai instruments bootstrap --provider databento "
                        f"--symbols {sym.rpartition('.')[0]}`."
                    ) from exc

                # First try the exchange-name canonical form (the form the
                # writer persists under for fresh registries + live-resolver
                # compat).
                idef = await registry.find_by_alias(
                    lookup_alias, provider="databento", as_of_date=as_of
                )
                if idef is not None:
                    out.append(lookup_alias)
                    continue

                # Historical-alias fallback. When the registry holds a
                # pre-normalization MIC alias (``AAPL.XNAS``) for a window
                # the user's start_date falls in — e.g. a venue migration
                # where the same definition had ``XNAS`` historical and
                # ``NASDAQ`` current — the canonical-form lookup above
                # misses the date but the ORIGINAL input form still hits.
                # Falling back to ``sym`` (un-normalized) covers this
                # without softening the unknown-suffix fail-loud contract:
                # by the time we get here, ``sym`` already passed
                # ``normalize_databento_alias_for_lookup`` validation.
                # E2E-discovered regression 2026-05-12 (fresh-VM-data-path-
                # closure verify-e2e report UC1/UC2): without this
                # fallback, every historical backtest using the MIC form
                # printed by ``msai ingest stocks`` 422s, AND every
                # exchange-name backtest with start_date predating the
                # current alias's ``effective_from`` 422s — a strict
                # regression from the prior exact-match resolver.
                if sym != lookup_alias:
                    idef = await registry.find_by_alias(sym, provider="databento", as_of_date=as_of)
                    if idef is not None:
                        # Return the FORM that actually matched, so
                        # downstream callers (catalog reads, Nautilus
                        # InstrumentId construction) get the venue suffix
                        # the registry row was written with.
                        out.append(sym)
                        continue

                # Path 2c — raw_symbol fallback. The user gave a dotted form
                # but neither the canonical nor the original-input form found
                # a row covering ``as_of``. This commonly happens when:
                # (a) the input has the exchange-name suffix (``AAPL.NASDAQ``)
                #     but the registry holds only the MIC form (``AAPL.XNAS``)
                #     because the bootstrap was run before
                #     ``venue_normalization`` shipped, OR
                # (b) the input has a MIC venue that maps to the same
                #     exchange-name as a registry row, but the multi-MIC →
                #     same-name compression (``XARC``/``ARCX`` → ``ARCA``)
                #     means reverse-mapping is ambiguous and we can't try
                #     all pre-images blindly.
                # ``find_by_raw_symbol`` resolves the symbol by its ticker
                # alone and returns whatever alias is active on ``as_of``.
                # Returning the registry's active alias (rather than echoing
                # the user input) keeps downstream catalog reads aligned with
                # the row that actually holds data. Discovered by verify-e2e
                # pass-2 UC2 on 2026-05-12.
                raw_root = sym.rpartition(".")[0]
                idef = await registry.find_by_raw_symbol(raw_root, provider="databento")
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
                        # Venue-mismatch guard (Codex P2 catch, PR #61 round 4):
                        # path 2c is only "right" when the user's input alias
                        # refers to the SAME instrument the registry holds —
                        # just under a different Databento-MIC vs exchange-name
                        # spelling (``AAPL.XNAS`` ↔ ``AAPL.NASDAQ``). If the
                        # user types a venue that names a DIFFERENT instrument
                        # (e.g. ``AAPL.NYSE`` — AAPL is on NASDAQ, not NYSE),
                        # silently returning the NASDAQ alias would let the
                        # backtest run against the wrong-venue contract
                        # without surfacing the operator error. Compare the
                        # normalized forms: if the active alias's normalized
                        # exchange-name differs from the user input's
                        # normalized form, fail loud with a hint.
                        try:
                            active_normalized = normalize_databento_alias_for_lookup(
                                active_alias.alias_string
                            )
                        except UnknownDatabentoVenueError:
                            active_normalized = active_alias.alias_string
                        if active_normalized == lookup_alias:
                            out.append(active_alias.alias_string)
                            continue
                        raise DatabentoDefinitionMissing(
                            f"Venue mismatch: requested alias {sym!r} "
                            f"(normalized to {lookup_alias!r}) but the registry "
                            f"row for raw_symbol {raw_root!r} resolves to "
                            f"{active_alias.alias_string!r} (normalized: "
                            f"{active_normalized!r}). Different venue → different "
                            f"instrument. Either correct the venue suffix or "
                            f"run `msai instruments bootstrap --provider "
                            f"databento --symbols {sym}` if {sym!r} is a "
                            f"distinct symbol that needs its own registry row."
                        )

                raise DatabentoDefinitionMissing(
                    f"No registry row for alias {sym!r} (tried "
                    f"{lookup_alias!r}, {sym!r}, and raw_symbol "
                    f"{raw_root!r} as of {as_of.isoformat()}) under provider "
                    f"'databento' — run `msai instruments bootstrap "
                    f"--provider databento --symbols {raw_root}` to register "
                    f"the symbol, then re-submit the backtest."
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
                f"'databento' — run `msai instruments bootstrap --provider "
                f"databento --symbols {sym}` to register the symbol, then "
                f"re-submit the backtest."
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
            DatabentoClientUnavailableError: ``self._databento`` is
                ``None`` on cold-miss — cannot fetch the definition
                payload.
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
            raise DatabentoClientUnavailableError(
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
    def _asset_class_for_instrument(instrument: Any) -> RegistryAssetClass:
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

    async def asset_class_for_alias(self, alias_str: str) -> IngestAssetClass | None:
        """Canonical alias → ingest-taxonomy asset_class.

        Looks up the alias in the registry under ``provider="interactive_brokers"``
        (using :func:`exchange_local_today` for windowing) and translates
        the definition's ``asset_class`` field (``equity`` / ``futures`` /
        ``fx`` / ``option`` / ``crypto`` per the
        ``ck_instrument_definitions_asset_class`` CHECK) to the ingest /
        Parquet-storage taxonomy (``stocks`` / ``futures`` / ``options`` /
        ``forex`` / ``crypto``) via :data:`_REGISTRY_TO_INGEST_ASSET_CLASS`.

        This mapping is critical — if the wrong name reaches
        ``DataIngestionService._resolve_plan`` the Parquet writes go
        to the wrong directory (e.g. ``data/parquet/equity/``) while
        the catalog reader expects ``data/parquet/stocks/``, producing
        a perpetual auto-heal loop.

        Returns ``None`` when the alias is empty, has no registry row,
        or has an unrecognized registry taxonomy. Callers fall back to
        the shape heuristic in
        :func:`msai.services.backtests.derive_asset_class.derive_asset_class_sync`.

        Narrow exception handling: SQLAlchemy errors (DB hiccup) and
        :class:`AmbiguousSymbolError` (legitimate registry signal that the
        caller can't disambiguate without ``asset_class``) are swallowed
        with a warning so the auto-heal pipeline doesn't crash; programmer
        errors (``AssertionError``, ``ImportError``, ``TypeError``) propagate.
        """
        if not alias_str:
            return None

        from sqlalchemy.exc import SQLAlchemyError

        from msai.services.nautilus.live_instrument_bootstrap import (
            exchange_local_today,
        )
        from msai.services.nautilus.security_master.registry import (
            AmbiguousSymbolError,
            InstrumentRegistry,
        )

        today = exchange_local_today()
        registry = InstrumentRegistry(self._db)
        try:
            idef = await registry.find_by_alias(
                alias_str,
                provider="interactive_brokers",
                as_of_date=today,
            )
        except (SQLAlchemyError, AmbiguousSymbolError):
            log.warning(
                "asset_class_for_alias_registry_lookup_failed",
                alias=alias_str,
                exc_info=True,
            )
            return None

        if idef is None:
            return None

        # ``InstrumentDefinition.asset_class`` is a generic ``str`` at the SQLA
        # type-stub level; the DB CHECK constraint keeps it within the registry
        # taxonomy. ``.get`` returns ``None`` for any unrecognized value, so an
        # off-list row from a future schema drift gracefully degrades to the
        # shape-heuristic fallback rather than raising.
        return _REGISTRY_TO_INGEST_ASSET_CLASS.get(idef.asset_class)  # type: ignore[call-overload,no-any-return]

    async def list_registered_instruments(
        self,
        *,
        asset_class: str | None = None,
    ) -> list[_RegisteredInstrument]:
        """Return all instruments with at least one currently-active alias,
        grouped by definition with an aggregated ``live_qualified`` flag.

        Used by the bulk inventory endpoint at
        ``GET /api/v1/symbols/inventory``. Filters out
        ``hidden_from_inventory`` rows (B6a soft-delete).

        v1 trade-off: ``last_refresh_at = InstrumentDefinition.updated_at``.
        This reflects ANY row mutation (alias rotation, registration
        correction), not strictly successful data downloads. Acceptable for
        v1's expected 30-80 row inventory. Deferred follow-up: denormalized
        ``last_refresh`` column updated by the worker on successful runs.
        """
        from sqlalchemy import func

        from msai.models.instrument_alias import InstrumentAlias
        from msai.models.instrument_definition import InstrumentDefinition
        from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today

        ib_present_expr = func.bool_or(InstrumentAlias.provider == "interactive_brokers").label(
            "live_qualified"
        )

        # Iter-1 review fix (P2-4): use Chicago "today" — the project invariant
        # for alias windowing per `feedback_alias_windowing_must_use_exchange_local_today`.
        # ``func.current_date()`` evaluates in Postgres server tz; matching the
        # writer side (and find_active_aliases) keeps the boundary uniform.
        today = exchange_local_today()

        stmt = (
            select(
                InstrumentDefinition.instrument_uid,
                InstrumentDefinition.raw_symbol,
                InstrumentDefinition.asset_class,
                InstrumentDefinition.provider,
                InstrumentDefinition.updated_at,
                ib_present_expr,
            )
            .join(
                InstrumentAlias,
                InstrumentAlias.instrument_uid == InstrumentDefinition.instrument_uid,
            )
            .where(InstrumentAlias.effective_from <= today)
            .where(InstrumentAlias.effective_to.is_(None) | (InstrumentAlias.effective_to > today))
            .where(InstrumentDefinition.hidden_from_inventory.is_(False))
            .group_by(
                InstrumentDefinition.instrument_uid,
                InstrumentDefinition.raw_symbol,
                InstrumentDefinition.asset_class,
                InstrumentDefinition.provider,
                InstrumentDefinition.updated_at,
            )
        )
        if asset_class is not None:
            stmt = stmt.where(InstrumentDefinition.asset_class == asset_class)
        stmt = stmt.order_by(InstrumentDefinition.raw_symbol)

        result = await self._db.execute(stmt)
        rows = result.all()

        return [
            _RegisteredInstrument(
                instrument_uid=r.instrument_uid,
                raw_symbol=r.raw_symbol,
                asset_class=r.asset_class,
                provider=r.provider,
                live_qualified=bool(r.live_qualified),
                last_refresh_at=r.updated_at,
            )
            for r in rows
        ]

    async def find_active_aliases(
        self,
        *,
        symbol: str,
        asset_class: RegistryAssetClass,
        as_of_date: date,
    ) -> AliasResolution:
        """Aggregate readiness view for a ``(symbol, asset_class)`` pair.

        Selects all active :class:`InstrumentAlias` rows (``effective_from
        <= as_of_date AND (effective_to IS NULL OR effective_to >
        as_of_date)``) that share the same ``raw_symbol`` and that hang
        off an :class:`InstrumentDefinition` with the requested
        ``asset_class``.

        ``asset_class`` is the registry/user-facing taxonomy
        (``equity`` | ``futures`` | ``fx`` | ``option`` | ``crypto``) per
        the ``ck_instrument_definitions_asset_class`` CHECK.

        ``as_of_date`` is required (no implicit ``date.today()``) so the
        caller owns the time-zone decision — same discipline as
        :meth:`InstrumentRegistry.find_by_alias` after PR #37.

        Raises :class:`AmbiguousSymbolError` when more than one
        :class:`InstrumentDefinition` row matches (same raw_symbol and
        asset_class but different ``instrument_uid`` across providers).
        Returns an :class:`AliasResolution` with ``instrument_uid=None``
        when no row matches — caller turns that into HTTP 404.

        Provider-preference policy: ``databento`` >
        ``interactive_brokers`` > anything else (deterministic fallback
        to the lexicographically-first remaining provider). The
        ``has_ib_alias`` flag is independent and reflects whether ANY
        active alias row carries ``provider="interactive_brokers"`` —
        that is the live-qualification signal for the Symbol Onboarding
        readiness contract.
        """
        from msai.models.instrument_alias import InstrumentAlias
        from msai.models.instrument_definition import InstrumentDefinition
        from msai.services.nautilus.security_master.registry import (
            AmbiguousSymbolError,
        )

        rows = (
            (
                await self._db.execute(
                    select(InstrumentAlias)
                    .join(
                        InstrumentDefinition,
                        InstrumentDefinition.instrument_uid == InstrumentAlias.instrument_uid,
                    )
                    .where(InstrumentDefinition.raw_symbol == symbol)
                    .where(InstrumentDefinition.asset_class == asset_class)
                    .where(InstrumentAlias.effective_from <= as_of_date)
                    .where(
                        (InstrumentAlias.effective_to.is_(None))
                        | (InstrumentAlias.effective_to > as_of_date)
                    )
                )
            )
            .scalars()
            .all()
        )

        if not rows:
            return AliasResolution(
                instrument_uid=None,
                primary_provider="",
                has_ib_alias=False,
                registry_asset_class=asset_class,
            )

        uids = {r.instrument_uid for r in rows}
        if len(uids) > 1:
            sorted_providers = sorted({r.provider for r in rows})
            # ``AmbiguousSymbolError.provider`` is typed as :data:`Provider`
            # (registry-namespaced); the SQLA-stub-typed ``r.provider`` is
            # ``str``. DB rows reach this branch only after passing the
            # ``ck_instrument_aliases_provider`` CHECK, so the cast is
            # invariant-preserving.
            first_provider = cast(
                "Provider",
                sorted_providers[0] if sorted_providers else "interactive_brokers",
            )
            raise AmbiguousSymbolError(
                symbol=symbol,
                provider=first_provider,
                asset_classes=[asset_class],
            )
        instrument_uid = next(iter(uids))
        provider_set = {r.provider for r in rows}
        has_ib = "interactive_brokers" in provider_set
        if "databento" in provider_set:
            primary = "databento"
        elif has_ib:
            primary = "interactive_brokers"
        else:
            primary = sorted(provider_set)[0]

        return AliasResolution(
            instrument_uid=instrument_uid,
            primary_provider=primary,
            has_ib_alias=has_ib,
            registry_asset_class=asset_class,
        )

    async def _upsert_definition_and_alias(
        self,
        *,
        raw_symbol: str,
        listing_venue: str,
        routing_venue: str,
        asset_class: RegistryAssetClass,
        alias_string: str,
        provider: Provider = "interactive_brokers",
        venue_format: VenueFormat = "exchange_name",
        source_venue_raw: str | None = None,
        trading_hours: dict[str, Any] | None = None,
    ) -> None:
        """Idempotent upsert: one :class:`InstrumentDefinition` row +
        one active :class:`InstrumentAlias` row.

        Called from both the IB qualification path (provider defaults to
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
        collide on the unique constraints.
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

        from sqlalchemy import func as _sa_func  # noqa: PLC0415
        from sqlalchemy import text as _sa_text  # noqa: PLC0415

        now = datetime.now(UTC)
        def_insert = pg_insert(InstrumentDefinition).values(
            raw_symbol=raw_symbol,
            listing_venue=listing_venue,
            routing_venue=routing_venue,
            asset_class=asset_class,
            provider=provider,
            lifecycle_state="active",
            refreshed_at=now,
            trading_hours=trading_hours,
        )
        def_stmt = def_insert.on_conflict_do_update(
            constraint="uq_instrument_definitions_symbol_provider_asset",
            # Refresh venue fields on conflict so an alias move
            # (e.g. AAPL.NASDAQ → AAPL.ARCA) propagates to the
            # definition row, not just the alias table. Without
            # this, callers reading InstrumentDefinition get
            # permanently stale venue metadata after the first
            # venue change.
            #
            # trading_hours uses COALESCE(NULLIF(excluded, 'null'::jsonb),
            # current) so callers passing trading_hours=None do NOT clobber
            # existing rows (writers without IB contract details preserve
            # prior data). The NULLIF guard is required because asyncpg
            # binds Python ``None`` as the JSONB literal ``'null'``, which
            # is distinct from SQL NULL — plain COALESCE keeps the JSON
            # ``null`` and silently overwrites the existing row.
            set_={
                "refreshed_at": now,
                "listing_venue": listing_venue,
                "routing_venue": routing_venue,
                "trading_hours": _sa_func.coalesce(
                    _sa_func.nullif(
                        def_insert.excluded.trading_hours,
                        _sa_text("'null'::jsonb"),
                    ),
                    InstrumentDefinition.trading_hours,
                ),
            },
        ).returning(InstrumentDefinition.__table__.c.instrument_uid)
        result = await self._db.execute(def_stmt)
        instrument_uid = result.scalar_one()

        # Use exchange-local (America/Chicago) date for the alias window
        # so the freshly-inserted alias passes ``find_by_alias`` immediately
        # after this upsert returns. ``now.date()`` is UTC and would stamp
        # tomorrow's UTC date during late US-Central hours, leaving the
        # resolver (which evaluates ``as_of_date = exchange_local_today()``,
        # still yesterday in Chicago) to compute ``effective_from > as_of_date``
        # → registry miss until the next Chicago calendar rollover.
        from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today

        today = exchange_local_today()

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
                # (idempotent same-day refresh path — the insert below
                # re-activates same-day rows via ON CONFLICT DO UPDATE).
                InstrumentAlias.alias_string != alias_string,
            )
            .values(effective_to=today)
        )
        await self._db.execute(close_stmt)

        # Asset-class-aware effective_from. NautilusTrader's own instrument
        # model gives ``FuturesContract`` / ``OptionContract`` / ``*_spread``
        # an ``expiration_ns`` field (time-bounded identity) but Equity / FX
        # (CurrencyPair) / Index / Crypto-perpetual / CFD have NO lifecycle
        # dates — they're time-invariant. Empirically verified in
        # ``.venv/lib/python3.12/site-packages/nautilus_trader/model/
        # instruments/{equity,futures_contract,currency_pair}.pyx`` on
        # 2026-05-12.
        #
        # Our registry uses ``effective_from/effective_to`` to model alias
        # lifecycle. That's correct for futures (``ESH4.GLBX`` IS bounded by
        # contract expiry) but wrong for equities (``AAPL.NASDAQ`` applies to
        # all time). Stamping ``effective_from=today`` for equities makes
        # historical backtests with ``start_date < today`` cold-miss the
        # alias windowing filter even though the alias is conceptually
        # always active. Discovered when the prod AAPL backtest with
        # ``start_date=2025-11-03`` 422-ed against an alias created today.
        #
        # Fix: time-invariant asset classes (equity / fx / crypto — the
        # last covers crypto-perpetual; crypto-futures aren't currently
        # in this codebase's asset_class taxonomy) get a far-past anchor.
        # Futures / options keep ``today`` so the roll history is precise.
        time_invariant_asset_classes: frozenset[str] = frozenset({"equity", "fx", "crypto"})
        far_past_anchor: date = date(1900, 1, 1)
        effective_from = far_past_anchor if asset_class in time_invariant_asset_classes else today

        # Alias upsert — re-activate same-day-same-alias rows (set effective_to
        # back to NULL) instead of ON CONFLICT DO NOTHING.
        #
        # E2E rerun fix (2026-05-01): a sequence A → B → A within one Chicago
        # day previously trapped state into ZERO active aliases. The third
        # call's close_stmt closed B (because effective_to IS NULL and
        # alias_string != A) but the A insert was a no-op due to the previous
        # uniqueness ON CONFLICT DO NOTHING — A was already in the table from
        # call 1, just with effective_to set by call 2's close. Switching to
        # ON CONFLICT DO UPDATE SET effective_to=NULL revives A as the single
        # active alias.
        alias_stmt = (
            pg_insert(InstrumentAlias)
            .values(
                instrument_uid=instrument_uid,
                alias_string=alias_string,
                venue_format=venue_format,
                provider=provider,
                effective_from=effective_from,
                effective_to=None,
                source_venue_raw=source_venue_raw,
            )
            .on_conflict_do_update(
                constraint="uq_instrument_aliases_string_provider_from",
                set_={"effective_to": None},
            )
        )
        await self._db.execute(alias_stmt)
        await self._db.flush()

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
