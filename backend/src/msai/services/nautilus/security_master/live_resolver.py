"""Pure-read live-start instrument resolver.

Council verdict 2026-04-19 (docs/decisions/live-path-registry-wiring.md)
mandates: registry-only, no IB qualifier, no upserts. Cold-miss is operator
action (`msai instruments refresh`). This module is the runtime entrypoint
for `/api/v1/live/start-portfolio` → supervisor → IB preload.

Extending to options requires adding option-specific fields to
``contract_spec`` (expiry, strike, right) — the resolver signature and
``ResolvedInstrument`` shape do NOT change.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from msai.services.alerting import (
    _HISTORY_EXECUTOR,  # noqa: SLF001 — private-by-convention; intentional coupling to alerting.py:305-328 bounded-write pattern
    _HISTORY_WRITE_TIMEOUT_S,  # noqa: SLF001
    alerting_service,
)
from msai.services.observability.trading_metrics import (
    LIVE_INSTRUMENT_RESOLVED_TOTAL,
)

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.models.instrument_alias import InstrumentAlias
    from msai.models.instrument_definition import InstrumentDefinition

_log = structlog.get_logger(__name__)
# Sibling stdlib logger used only for the ``_fire_alert_bounded`` timeout /
# exception paths so they emit via the standard logging channel (matches
# alerting.py:242-256 ``_log_history_failure`` pattern).
_alert_log = logging.getLogger(__name__)


async def _fire_alert_bounded(
    level: str,
    title: str,
    message: str,
) -> None:
    """Bounded file-lock alert write.

    Matches the pattern in ``alerting.py:305-328`` — offload the sync
    ``alerting_service.send_alert`` call to the shared single-thread
    history executor and cap the wait so a wedged alerts volume cannot
    hang the live-start critical path. The alert task runs AT MOST
    ``_HISTORY_WRITE_TIMEOUT_S`` (typically 2s); on timeout, a
    done-callback consumes the late future's result so the error is
    observable but non-blocking.
    """
    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(
        _HISTORY_EXECUTOR,
        alerting_service.send_alert,
        level,
        title,
        message,
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=_HISTORY_WRITE_TIMEOUT_S,
        )
    except TimeoutError:
        _alert_log.warning(
            "alert_history_write_timed_out",
            extra={"title": title, "timeout_s": _HISTORY_WRITE_TIMEOUT_S},
        )

        def _log_late(fut: asyncio.Future[None], _title: str = title) -> None:
            try:
                fut.result()
            except Exception:  # noqa: BLE001 — best-effort drain of late future
                _alert_log.exception(
                    "alert_history_late_failed",
                    extra={"title": _title},
                )
            else:
                _alert_log.info(
                    "alert_history_late_complete",
                    extra={"title": _title},
                )

        task.add_done_callback(_log_late)
    except Exception:
        _alert_log.exception("alert_history_write_failed", extra={"title": title})


class AssetClass(StrEnum):
    EQUITY = "equity"
    FUTURES = "futures"
    FX = "fx"
    OPTION = "option"
    CRYPTO = "crypto"


class AmbiguityReason(StrEnum):
    """Why ``lookup_for_live`` couldn't deterministically pick one alias."""

    CROSS_ASSET_CLASS = "cross_asset_class"
    SAME_DAY_OVERLAP = "same_day_overlap"


class TelemetrySource(StrEnum):
    """``source`` label values for ``live_instrument_resolved`` log +
    ``msai_live_instrument_resolved_total`` counter."""

    REGISTRY = "registry"
    REGISTRY_MISS = "registry_miss"
    REGISTRY_INCOMPLETE = "registry_incomplete"


@dataclass(frozen=True)
class ResolvedInstrument:
    """Result of a successful ``lookup_for_live`` resolution.

    ``contract_spec`` is opaque to the supervisor — the IB preload builder
    parses it into an ``IBContract``. Options extension adds new keys to
    ``contract_spec`` without changing this dataclass's shape.
    """

    canonical_id: str
    asset_class: AssetClass
    contract_spec: dict[str, Any]
    effective_window: tuple[date, date | None]


class LiveResolverError(ValueError):
    """Base for typed errors from :func:`lookup_for_live`.

    Subclasses ``ValueError`` so the supervisor's payload-factory
    catch in ``ProcessManager.spawn()`` treats resolver failures as
    permanent (no XAUTOCLAIM retry). ``ProcessManager`` dispatches on
    exception type before calling ``_mark_failed`` so each subclass
    maps to a distinct :class:`FailureKind`.

    Every subclass exposes :meth:`to_error_message` which returns a
    JSON string round-trippable to structured details at the API
    boundary (``EndpointOutcome.registry_permanent_failure``). Format:
    ``'{"code": "<CODE>", "message": "...", "details": {...}}'``.
    """

    def to_error_message(self) -> str:  # overridden by subclasses
        return json.dumps({"code": "LIVE_RESOLVER_ERROR", "message": str(self), "details": {}})


class RegistryMissError(LiveResolverError):
    """Raised when one or more symbols have no active registry alias.

    Error message includes a copy-pastable ``msai instruments refresh``
    command so the operator can self-correct in seconds.
    """

    def __init__(self, symbols: list[str], as_of_date: date) -> None:
        self.symbols = symbols
        self.as_of_date = as_of_date
        joined = ",".join(symbols)
        super().__init__(
            f"Symbol(s) not in registry: {symbols!r} as of {as_of_date.isoformat()}. "
            f"Run: msai instruments refresh --symbols {joined} "
            "--provider interactive_brokers"
        )

    def to_error_message(self) -> str:
        return json.dumps(
            {
                "code": "REGISTRY_MISS",
                "message": str(self),
                "details": {
                    "missing_symbols": self.symbols,
                    "as_of_date": self.as_of_date.isoformat(),
                },
            }
        )


class RegistryIncompleteError(LiveResolverError):
    """Raised when a registry row is missing a required field."""

    def __init__(self, symbol: str, missing_field: str) -> None:
        self.symbol = symbol
        self.missing_field = missing_field
        super().__init__(
            f"Registry row for {symbol!r} is incomplete: missing {missing_field!r}. "
            "This is a data-integrity issue — re-run `msai instruments refresh`."
        )

    def to_error_message(self) -> str:
        return json.dumps(
            {
                "code": "REGISTRY_INCOMPLETE",
                "message": str(self),
                "details": {"symbol": self.symbol, "missing_field": self.missing_field},
            }
        )


class UnsupportedAssetClassError(LiveResolverError):
    """Raised when the resolved asset_class is not wired for live trading yet."""

    def __init__(self, symbol: str, asset_class: AssetClass) -> None:
        self.symbol = symbol
        self.asset_class = asset_class
        super().__init__(
            f"Symbol {symbol!r} resolved to asset_class={asset_class.value!r} "
            "which is not yet supported for live trading. Supported: equity, futures, fx."
        )

    def to_error_message(self) -> str:
        return json.dumps(
            {
                "code": "UNSUPPORTED_ASSET_CLASS",
                "message": str(self),
                "details": {"symbol": self.symbol, "asset_class": self.asset_class.value},
            }
        )


class AmbiguousRegistryError(LiveResolverError):
    """Raised when the resolver cannot deterministically pick a single
    registry row/alias for a symbol. Two sources:

    1. **Cross-asset-class:** a bare symbol matches multiple
       ``instrument_definitions`` rows across asset_classes (e.g. SPY
       as equity AND option underlying). Wraps the registry-layer
       ``AmbiguousSymbolError`` so it flows through the ``ValueError``
       permanent-catch (instead of the transient-retry branch that
       would catch a bare ``AmbiguousSymbolError``).

    2. **Same-day overlap:** multiple active aliases share the same
       (maximum) ``effective_from`` date — operator-seeded data-
       integrity issue; no deterministic PRD tie-break rule applies.

    Consumers differentiate via ``reason`` attribute.
    """

    # Legacy string aliases kept so existing call sites that read
    # `AmbiguousRegistryError.REASON_CROSS_ASSET_CLASS` still work.
    # New code should use the `AmbiguityReason` enum at module scope.
    REASON_CROSS_ASSET_CLASS = AmbiguityReason.CROSS_ASSET_CLASS.value
    REASON_SAME_DAY_OVERLAP = AmbiguityReason.SAME_DAY_OVERLAP.value

    def __init__(
        self,
        symbol: str,
        conflicts: list[str],
        reason: AmbiguityReason | str,
    ) -> None:
        self.symbol = symbol
        self.conflicts = sorted(conflicts)
        self.reason = AmbiguityReason(reason)
        if self.reason is AmbiguityReason.CROSS_ASSET_CLASS:
            msg = (
                f"Symbol {symbol!r} matches multiple registry definitions "
                f"across asset_classes {self.conflicts!r}; pin the "
                "asset_class by passing the dotted alias form (e.g. 'SPY.ARCA')."
            )
        else:
            msg = (
                f"Symbol {symbol!r} has multiple active aliases on the same "
                f"effective_from date: {self.conflicts!r}. Operator must "
                "close one alias row — re-run `msai instruments refresh` "
                "or manually set effective_to on the stale row."
            )
        super().__init__(msg)

    def to_error_message(self) -> str:
        return json.dumps(
            {
                "code": "AMBIGUOUS_REGISTRY",
                "message": str(self),
                "details": {
                    "symbol": self.symbol,
                    "reason": self.reason.value,
                    "conflicts": self.conflicts,
                },
            }
        )


# --- contract_spec construction ----------------------------------------
# IB futures month codes: F=Jan, G=Feb, H=Mar, J=Apr, K=May, M=Jun,
# N=Jul, Q=Aug, U=Sep, V=Oct, X=Nov, Z=Dec.
_FUTURES_MONTH_CODES = {
    "F": "01",
    "G": "02",
    "H": "03",
    "J": "04",
    "K": "05",
    "M": "06",
    "N": "07",
    "Q": "08",
    "U": "09",
    "V": "10",
    "X": "11",
    "Z": "12",
}


def _build_contract_spec(
    definition: InstrumentDefinition,
    alias: InstrumentAlias,
) -> dict[str, Any]:
    """Construct an IB-compatible contract_spec from a registry row pair.

    Returned dict is consumed by ``build_ib_instrument_provider_config_from_resolved``
    to reconstruct an ``IBContract``. Fields are IB SDK conventions (secType,
    symbol, exchange, primaryExchange, currency, lastTradeDateOrContractMonth).
    """
    if not definition.listing_venue:
        raise RegistryIncompleteError(definition.raw_symbol, "listing_venue")
    if not definition.routing_venue:
        raise RegistryIncompleteError(definition.raw_symbol, "routing_venue")

    ac = definition.asset_class
    if ac == AssetClass.EQUITY.value:
        return {
            "secType": "STK",
            "symbol": definition.raw_symbol,
            "exchange": definition.routing_venue,
            "primaryExchange": definition.listing_venue,
            "currency": "USD",
        }
    if ac == AssetClass.FX.value:
        if "/" not in definition.raw_symbol:
            raise RegistryIncompleteError(definition.raw_symbol, "raw_symbol.base_quote_split")
        base, quote = definition.raw_symbol.split("/", 1)
        if not base or not quote:
            raise RegistryIncompleteError(definition.raw_symbol, "raw_symbol.malformed")
        return {
            "secType": "CASH",
            "symbol": base,
            "exchange": definition.routing_venue,
            "currency": quote,
        }
    if ac == AssetClass.FUTURES.value:
        return {
            "secType": "FUT",
            "symbol": definition.raw_symbol,
            "exchange": definition.routing_venue,
            "lastTradeDateOrContractMonth": _parse_futures_expiry(
                alias.alias_string, alias.effective_from, definition.raw_symbol
            ),
            "currency": "USD",
        }
    # Option / crypto are raised at the lookup_for_live boundary, not here;
    # this branch is defensive.
    raise RegistryIncompleteError(definition.raw_symbol, f"asset_class={ac}")


def _parse_futures_expiry(
    alias_string: str,
    effective_from: date,
    raw_symbol: str,
) -> str:
    """Parse ``ESM6.CME`` -> ``'202606'``.

    Year disambiguation: pick the smallest year >= effective_from.year
    whose units digit matches ``year_digit``. This correctly handles
    decade-boundary rolls (effective_from=2029-12-15 + alias ``ESH0``
    -> 2030-03, not 2020-03).
    """
    root, _, _venue = alias_string.partition(".")
    # Strip the root symbol prefix (e.g. "ES" or "NQ") to isolate the
    # "M6" / "Z5" tail.
    if not root.startswith(raw_symbol):
        raise RegistryIncompleteError(raw_symbol, f"alias.root_mismatch: {alias_string!r}")
    tail = root[len(raw_symbol) :]
    if len(tail) != 2 or tail[0] not in _FUTURES_MONTH_CODES:
        raise RegistryIncompleteError(raw_symbol, f"alias.month_code: {alias_string!r}")
    month_code, year_digit_str = tail[0], tail[1]
    if not year_digit_str.isdigit():
        raise RegistryIncompleteError(raw_symbol, f"alias.year_digit: {alias_string!r}")
    year_digit = int(year_digit_str)
    base = effective_from.year
    base_decade = (base // 10) * 10
    candidate = base_decade + year_digit
    # If the in-decade candidate is already in the past relative to the
    # alias becoming active, the expiry is next decade.
    if candidate < base:
        candidate += 10
    return f"{candidate:04d}{_FUTURES_MONTH_CODES[month_code]}"


# --- Active-alias picker (provider-filtered + overlap-deterministic) ----


def _pick_active_alias(
    idef: InstrumentDefinition,
    *,
    provider: str,
    as_of_date: date,
    caller_symbol: str,
) -> InstrumentAlias | None:
    """Pick the active alias for ``idef`` under ``provider`` on ``as_of_date``.

    Filters by provider (the ORM relationship loads ALL providers) and by
    effective window. On window overlap, returns the alias with the most
    recent ``effective_from`` (PRD §4 US-003 tie-break rule).

    If multiple active aliases share the SAME (maximum) ``effective_from``,
    raises :class:`AmbiguousRegistryError` (reason=``SAME_DAY_OVERLAP``) —
    operator-seeded rows that need cleanup.
    """
    candidates = [
        a
        for a in idef.aliases
        if a.provider == provider
        and a.effective_from <= as_of_date
        and (a.effective_to is None or a.effective_to > as_of_date)
    ]
    if not candidates:
        return None
    # Sort by effective_from DESC. If multiple active aliases share the max
    # effective_from, that's an operator-seeded data-integrity issue — do
    # NOT silently pick one, raise AmbiguousRegistryError.
    candidates.sort(key=lambda a: a.effective_from, reverse=True)
    max_date = candidates[0].effective_from
    tied_at_max = [c for c in candidates if c.effective_from == max_date]
    if len(tied_at_max) > 1:
        # Deterministic reporting order for the error's ``conflicts`` list.
        tied_at_max.sort(key=lambda a: a.alias_string)
        raise AmbiguousRegistryError(
            symbol=caller_symbol,
            conflicts=[a.alias_string for a in tied_at_max],
            reason=AmbiguousRegistryError.REASON_SAME_DAY_OVERLAP,
        )
    return tied_at_max[0]


# --- Core resolver -----------------------------------------------------


async def lookup_for_live(
    symbols: list[str],
    *,
    as_of_date: date,
    session: AsyncSession,
    provider: str = "interactive_brokers",
) -> list[ResolvedInstrument]:
    """Pure-read registry resolver for the live-start critical path.

    Args:
        symbols: Non-empty list of raw tickers or dotted aliases. Dotted
            inputs (``"AAPL.NASDAQ"``) are looked up via
            :meth:`InstrumentRegistry.find_by_alias`; bare inputs
            (``"AAPL"``) via :meth:`find_by_raw_symbol`.
        as_of_date: Exchange-local date (America/Chicago). Required — no
            default, because ``find_by_alias`` requires it now
            (Task 3b removed the UTC fallback).
        session: Async DB session.
        provider: Registry provider namespace (default
            ``"interactive_brokers"`` — the only one wired for live).

    Returns:
        ``ResolvedInstrument`` per input symbol, order-preserved.

    Raises:
        ValueError: ``symbols`` is empty.
        TypeError: ``as_of_date`` is not a :class:`datetime.date`.
        RegistryMissError: One or more symbols have no active alias.
        RegistryIncompleteError: A matched row is missing required fields.
        UnsupportedAssetClassError: A matched row has ``asset_class`` of
            ``option`` / ``crypto`` (not wired for live yet).
        AmbiguousRegistryError: Cross-asset-class ambiguity OR same-day
            overlap in active aliases.
    """
    # Lazy runtime imports — declared in TYPE_CHECKING for mypy, needed
    # at runtime here for construction / exception catch.
    from datetime import date as _date
    from datetime import datetime as _dt

    from msai.services.nautilus.security_master.registry import (
        AmbiguousSymbolError,
        InstrumentRegistry,
    )

    if not symbols:
        raise ValueError("symbols cannot be empty")
    # Reject datetime explicitly — ``isinstance(x, date)`` is True for
    # datetime (subclass relationship), so check datetime first.
    if isinstance(as_of_date, _dt) or not isinstance(as_of_date, _date):
        raise TypeError(
            "as_of_date must be a datetime.date in America/Chicago "
            "semantics, not a datetime. Use exchange_local_today() or "
            "date.fromisoformat(spawn_today_iso)."
        )

    registry = InstrumentRegistry(session)
    resolved: list[ResolvedInstrument] = []
    missing: list[str] = []

    for sym in symbols:
        # Dispatch: try raw_symbol first; fall back to alias on miss for
        # dotted inputs. The "." in sym heuristic alone is unsafe —
        # NYSE share-class tickers (BRK.B, BF.B, RDS.A) and any other
        # raw_symbol that legitimately contains a period would be
        # routed to alias lookup and miss their own raw_symbol row.
        # raw_symbol is the primary operator-typed key; alias is the
        # pinned venue form.
        # The registry raises AmbiguousSymbolError (NOT a ValueError)
        # on a cross-asset-class raw_symbol match; we wrap it into
        # AmbiguousRegistryError (subclass of LiveResolverError →
        # ValueError) so the supervisor's permanent-catch fires instead
        # of the transient-retry branch.
        try:
            idef = await registry.find_by_raw_symbol(sym, provider=provider, asset_class=None)
        except AmbiguousSymbolError as exc:
            raise AmbiguousRegistryError(
                symbol=sym,
                conflicts=exc.asset_classes,
                reason=AmbiguousRegistryError.REASON_CROSS_ASSET_CLASS,
            ) from exc
        if idef is None and "." in sym:
            idef = await registry.find_by_alias(sym, provider=provider, as_of_date=as_of_date)
        if idef is None:
            missing.append(sym)
            continue

        # Validate asset class. Unknown value at the DB layer (the schema
        # CHECK constraint normally prevents this) → RegistryIncomplete.
        # Emit telemetry before the raise so the metric reflects every
        # incomplete resolution, not just the _build_contract_spec path.
        try:
            ac = AssetClass(idef.asset_class)
        except ValueError as e:
            _log.error(
                "live_instrument_resolved",
                source=TelemetrySource.REGISTRY_INCOMPLETE.value,
                symbol=sym,
                missing_field=f"asset_class={idef.asset_class!r}",
                as_of_date=as_of_date.isoformat(),
            )
            LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
                source=TelemetrySource.REGISTRY_INCOMPLETE.value,
                asset_class="unknown",
            ).inc()
            # No alert for enum-conversion — the DB CHECK constraint should
            # prevent it reaching this path. If it does, the DB-schema
            # alert is the right surface, not a resolver alert.
            raise RegistryIncompleteError(sym, f"asset_class={idef.asset_class!r}") from e
        if ac in (AssetClass.OPTION, AssetClass.CRYPTO):
            raise UnsupportedAssetClassError(sym, ac)

        # Pick the active alias (provider-filtered + overlap-deterministic).
        active_alias = _pick_active_alias(
            idef,
            provider=provider,
            as_of_date=as_of_date,
            caller_symbol=sym,
        )
        if active_alias is None:
            missing.append(sym)
            continue

        # Wrap _build_contract_spec so a RegistryIncompleteError from a
        # bad row emits telemetry + fires an ERROR alert before the raise
        # propagates to the supervisor.
        try:
            spec = _build_contract_spec(idef, active_alias)
        except RegistryIncompleteError as inc_exc:
            _log.error(
                "live_instrument_resolved",
                source=TelemetrySource.REGISTRY_INCOMPLETE.value,
                symbol=sym,
                missing_field=inc_exc.missing_field,
                as_of_date=as_of_date.isoformat(),
            )
            LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
                source=TelemetrySource.REGISTRY_INCOMPLETE.value,
                asset_class=ac.value,
            ).inc()
            # Fire-and-forget — the alert is observability; its
            # completion is not required for the raise. Blocking up
            # to 2s here would add latency on the already-failing
            # path. The bounded helper's own timeout + done-callback
            # drain any late completion.
            asyncio.create_task(
                _fire_alert_bounded(
                    "error",
                    "Live instrument registry incomplete",
                    str(inc_exc),
                )
            )
            raise
        resolved.append(
            ResolvedInstrument(
                canonical_id=active_alias.alias_string,
                asset_class=ac,
                contract_spec=spec,
                effective_window=(
                    active_alias.effective_from,
                    active_alias.effective_to,
                ),
            )
        )
        _log.info(
            "live_instrument_resolved",
            source=TelemetrySource.REGISTRY.value,
            symbol=sym,
            canonical_id=active_alias.alias_string,
            asset_class=ac.value,
            as_of_date=as_of_date.isoformat(),
        )
        LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
            source=TelemetrySource.REGISTRY.value,
            asset_class=ac.value,
        ).inc()

    if missing:
        for m in missing:
            _log.warning(
                "live_instrument_resolved",
                source=TelemetrySource.REGISTRY_MISS.value,
                symbol=m,
                as_of_date=as_of_date.isoformat(),
            )
            LIVE_INSTRUMENT_RESOLVED_TOTAL.labels(
                source=TelemetrySource.REGISTRY_MISS.value,
                asset_class="unknown",
            ).inc()

        alert_message = (
            f"Registry miss on symbols {missing!r} as of "
            f"{as_of_date.isoformat()}. Run: msai instruments refresh "
            f"--symbols {','.join(missing)} --provider interactive_brokers"
        )
        # Fire-and-forget — see note in the incomplete path above.
        asyncio.create_task(
            _fire_alert_bounded(
                "warning",
                "Live instrument registry miss",
                alert_message,
            )
        )
        raise RegistryMissError(symbols=missing, as_of_date=as_of_date)

    return resolved
