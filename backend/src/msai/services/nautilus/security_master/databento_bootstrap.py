"""On-demand Databento registry bootstrap for equities, ETFs, and futures.

Entry point for both the HTTP API (POST /api/v1/instruments/bootstrap)
and the CLI (msai instruments bootstrap). Reuses SecurityMaster's write
path (_upsert_definition_and_alias) and returns per-symbol outcomes with
explicit readiness-state flags.

Session-per-symbol: the service takes an ``async_sessionmaker`` and opens
a new session + transaction per symbol. AsyncSession is NOT safe for
concurrent task use.

Contract:
- Databento-bootstrapped rows are backtest-discoverable ONLY. Live
  graduation requires a separate IB refresh.
- Per-symbol outcomes: CREATED / NOOP / ALIAS_ROTATED / AMBIGUOUS /
  UPSTREAM_ERROR / UNAUTHORIZED / UNMAPPED_VENUE / RATE_LIMITED.
- Batch: max_concurrent hard-capped at 3.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import SQLAlchemyError

from msai.core.logging import get_logger
from msai.services.observability.trading_metrics import (
    REGISTRY_BOOTSTRAP_DURATION_MS,
    REGISTRY_BOOTSTRAP_TOTAL,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from msai.services.data_sources.databento_client import DatabentoClient

log = get_logger(__name__)


class BootstrapOutcome(enum.StrEnum):
    CREATED = "created"
    NOOP = "noop"
    ALIAS_ROTATED = "alias_rotated"
    AMBIGUOUS = "ambiguous"
    UPSTREAM_ERROR = "upstream_error"
    UNAUTHORIZED = "unauthorized"
    UNMAPPED_VENUE = "unmapped_venue"
    RATE_LIMITED = "rate_limited"


# Outcomes that indicate the symbol was successfully registered; used by
# response-summary aggregation and invariant checks on BootstrapResult.
SUCCESSFUL_OUTCOMES: frozenset[BootstrapOutcome] = frozenset(
    {BootstrapOutcome.CREATED, BootstrapOutcome.NOOP, BootstrapOutcome.ALIAS_ROTATED}
)

# Outcome-severity ranking for dataset-fallback tie-breaking. When the
# equity loop tries 3 datasets and multiple fail, the HIGHEST-severity
# failure is surfaced so operators get the most actionable diagnostic
# (UNAUTHORIZED > UPSTREAM_ERROR > others).
_OUTCOME_SEVERITY: dict[BootstrapOutcome, int] = {
    BootstrapOutcome.UNAUTHORIZED: 3,
    BootstrapOutcome.RATE_LIMITED: 2,
    BootstrapOutcome.UPSTREAM_ERROR: 1,
    BootstrapOutcome.UNMAPPED_VENUE: 1,
}


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    symbol: str
    outcome: BootstrapOutcome
    registered: bool
    backtest_data_available: bool | None
    live_qualified: bool
    canonical_id: str | None = None
    dataset: str | None = None
    asset_class: str | None = None
    candidates: list[dict[str, str]] = field(default_factory=list)
    diagnostics: str | None = None

    def __post_init__(self) -> None:
        # Cross-field invariants: registered ⇔ outcome in SUCCESSFUL_OUTCOMES;
        # failed outcomes have live_qualified=False and backtest_data_available=False.
        is_success = self.outcome in SUCCESSFUL_OUTCOMES
        if is_success != self.registered:
            raise ValueError(
                f"BootstrapResult invariant: outcome={self.outcome!r} "
                f"but registered={self.registered!r}"
            )
        if not is_success:
            if self.live_qualified:
                raise ValueError(
                    f"BootstrapResult invariant: failed outcome {self.outcome!r} "
                    "cannot have live_qualified=True"
                )
            if self.backtest_data_available:
                raise ValueError(
                    f"BootstrapResult invariant: failed outcome {self.outcome!r} "
                    "cannot have backtest_data_available=True"
                )
        if self.outcome == BootstrapOutcome.AMBIGUOUS and len(self.candidates) < 2:
            raise ValueError("AMBIGUOUS outcome requires at least 2 candidates")


# Equity datasets probed in order on cold-miss.
_EQUITY_DATASETS: tuple[str, ...] = ("XNAS.ITCH", "XNYS.PILLAR", "ARCX.PILLAR")
_FUTURES_DATASET = "GLBX.MDP3"


def _safe_filename(symbol: str) -> str:
    """Deterministic filename derived from the symbol's sha1 digest.

    Bootstrap requests validate `symbols` against a regex that still
    admits `.`, `/`, and `-`; using the raw symbol as a path segment
    would let `../../evil` traverse out of the temp directory and write
    to an arbitrary location. A hex digest is collision-free for our
    input alphabet and preserves no user-controlled characters.
    """
    digest = hashlib.sha1(symbol.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]
    return f"databento-{digest}.definition.dbn.zst"


def _pick_highest_severity(
    current: BootstrapResult | None, candidate: BootstrapResult
) -> BootstrapResult:
    """Keep the most actionable failure outcome across a dataset fallback loop."""
    if current is None:
        return candidate
    return (
        candidate
        if _OUTCOME_SEVERITY.get(candidate.outcome, 0) > _OUTCOME_SEVERITY.get(current.outcome, 0)
        else current
    )


def _extract_venue(alias_string: str) -> str:
    """Return the venue suffix from a dotted alias (``AAPL.XNAS`` → ``XNAS``).

    Fail-loud on malformed inputs so a dot-less alias can never leak an
    empty string into ``InstrumentDefinition.listing_venue``.
    """
    root, sep, venue = alias_string.rpartition(".")
    if not sep or not venue:
        raise ValueError(
            f"Cannot extract venue from alias {alias_string!r}: expected '<root>.<venue>'"
        )
    return venue


class DatabentoBootstrapService:
    """Orchestrator for on-demand Databento registry bootstrap.

    Session-per-symbol: one ``AsyncSession`` is opened per symbol so that
    concurrent tasks under ``asyncio.gather`` don't share a session
    (SQLAlchemy's ``AsyncSession`` is not safe for concurrent task use).
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        databento_client: DatabentoClient,
        max_concurrent: int = 3,
    ) -> None:
        if not 1 <= max_concurrent <= 3:
            raise ValueError("max_concurrent must be 1..3")
        self._session_factory = session_factory
        self._databento = databento_client
        self._sem = asyncio.Semaphore(max_concurrent)

    async def bootstrap(
        self,
        *,
        symbols: list[str],
        asset_class_override: str | None,
        exact_ids: dict[str, str] | None,
    ) -> list[BootstrapResult]:
        """Bootstrap a batch of symbols. Returns one BootstrapResult per symbol.

        Unexpected exceptions inside per-symbol work are captured with
        ``return_exceptions=True`` and materialized as UPSTREAM_ERROR
        results so partial progress on other symbols in the batch is
        preserved.
        """
        exact_ids_map = exact_ids or {}
        tasks = [self._bootstrap_one(sym, asset_class_override, exact_ids_map) for sym in symbols]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[BootstrapResult] = []
        for sym, raw in zip(symbols, raw_results, strict=True):
            if isinstance(raw, BaseException):
                # Re-raise cancellation / keyboard / system exit so the
                # surrounding runtime sees them (gather swallows them
                # otherwise). Everything else materializes as a failure row.
                if isinstance(raw, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise raw
                log.error(
                    "databento_bootstrap_unexpected_exception",
                    symbol=sym,
                    exc_type=type(raw).__name__,
                    exc=str(raw),
                )
                results.append(
                    BootstrapResult(
                        symbol=sym,
                        outcome=BootstrapOutcome.UPSTREAM_ERROR,
                        registered=False,
                        backtest_data_available=False,
                        live_qualified=False,
                        diagnostics=f"{type(raw).__name__}: {raw}",
                    )
                )
            else:
                results.append(raw)
        return results

    async def _bootstrap_one(
        self,
        symbol: str,
        asset_class_override: str | None,
        exact_ids: dict[str, str],
    ) -> BootstrapResult:
        async with self._sem:
            start = time.perf_counter()
            try:
                from msai.services.nautilus.security_master.continuous_futures import (
                    is_databento_continuous_pattern,
                )

                if is_databento_continuous_pattern(symbol):
                    result = await self._bootstrap_continuous_future(symbol, asset_class_override)
                else:
                    result = await self._bootstrap_equity(
                        symbol, asset_class_override, exact_ids.get(symbol)
                    )
            finally:
                elapsed_ms = int((time.perf_counter() - start) * 1_000)
                REGISTRY_BOOTSTRAP_DURATION_MS.observe(elapsed_ms)

            REGISTRY_BOOTSTRAP_TOTAL.labels(
                provider="databento",
                asset_class=result.asset_class or "unknown",
                outcome=result.outcome.value,
            ).inc()
            return result

    async def _bootstrap_equity(
        self,
        symbol: str,
        asset_class_override: str | None,
        exact_id: str | None,
    ) -> BootstrapResult:
        from msai.services.data_sources.databento_client import AmbiguousDatabentoSymbolError
        from msai.services.data_sources.databento_errors import (
            DatabentoRateLimitedError,
            DatabentoUnauthorizedError,
            DatabentoUpstreamError,
        )
        from msai.services.nautilus.security_master.continuous_futures import (
            asset_class_for_instrument_type,
        )
        from msai.services.nautilus.security_master.service import SecurityMaster
        from msai.services.nautilus.security_master.venue_normalization import (
            UnknownDatabentoVenueError,
            normalize_alias_for_registry,
        )

        # Probe a short historical window ending yesterday, not today.
        # Databento's dataset ``available_end`` lags the UTC midnight
        # rollover by several hours; querying with ``start=today`` during
        # that window returns HTTP 422 ``data_start_after_available_end``
        # for every equity symbol. A -7d/-1d window is always inside the
        # published range for a Definition-schema probe (contract metadata
        # changes infrequently — any recent snapshot has the symbol).
        today = date.today()
        start_probe = (today - timedelta(days=7)).isoformat()
        end_probe = (today - timedelta(days=1)).isoformat()
        last_error: BootstrapResult | None = None

        for dataset in _EQUITY_DATASETS:
            with TemporaryDirectory() as tmpdir:
                target = Path(tmpdir) / _safe_filename(symbol)
                try:
                    instruments = await self._databento.fetch_definition_instruments(
                        symbol=symbol,
                        start=start_probe,
                        end=end_probe,
                        dataset=dataset,
                        target_path=target,
                        exact_id=exact_id,
                    )
                except AmbiguousDatabentoSymbolError as exc:
                    log.warning(
                        "databento_bootstrap_ambiguous",
                        symbol=symbol,
                        dataset=dataset,
                        candidate_count=len(exc.candidates),
                    )
                    return BootstrapResult(
                        symbol=symbol,
                        outcome=BootstrapOutcome.AMBIGUOUS,
                        registered=False,
                        backtest_data_available=False,
                        live_qualified=False,
                        candidates=exc.candidates,
                        dataset=dataset,
                    )
                except DatabentoUnauthorizedError as exc:
                    log.warning(
                        "databento_bootstrap_dataset_unauthorized",
                        symbol=symbol,
                        dataset=dataset,
                    )
                    last_error = _pick_highest_severity(
                        last_error,
                        BootstrapResult(
                            symbol=symbol,
                            outcome=BootstrapOutcome.UNAUTHORIZED,
                            registered=False,
                            backtest_data_available=False,
                            live_qualified=False,
                            diagnostics=str(exc),
                            dataset=dataset,
                        ),
                    )
                    continue
                except DatabentoRateLimitedError as exc:
                    log.warning(
                        "databento_bootstrap_rate_limited",
                        symbol=symbol,
                        dataset=dataset,
                    )
                    # Rate-limit short-circuits: retrying next dataset would
                    # re-trip the same upstream quota.
                    return BootstrapResult(
                        symbol=symbol,
                        outcome=BootstrapOutcome.RATE_LIMITED,
                        registered=False,
                        backtest_data_available=False,
                        live_qualified=False,
                        diagnostics=str(exc),
                        dataset=dataset,
                    )
                except DatabentoUpstreamError as exc:
                    log.warning(
                        "databento_bootstrap_dataset_upstream_error",
                        symbol=symbol,
                        dataset=dataset,
                        exc=str(exc),
                    )
                    last_error = _pick_highest_severity(
                        last_error,
                        BootstrapResult(
                            symbol=symbol,
                            outcome=BootstrapOutcome.UPSTREAM_ERROR,
                            registered=False,
                            backtest_data_available=False,
                            live_qualified=False,
                            diagnostics=str(exc),
                            dataset=dataset,
                        ),
                    )
                    continue

                if not instruments:
                    continue

                inst = instruments[0]
                alias_string = str(inst.id.value)
                raw_symbol_str = (
                    inst.raw_symbol.value
                    if hasattr(inst, "raw_symbol") and hasattr(inst.raw_symbol, "value")
                    else symbol
                )
                derived_asset_class = (
                    asset_class_override
                    if asset_class_override is not None
                    else asset_class_for_instrument_type(inst.__class__.__name__)
                )
                try:
                    listing_venue = _extract_venue(alias_string)
                except ValueError as exc:
                    log.warning(
                        "databento_bootstrap_malformed_alias",
                        symbol=symbol,
                        alias_string=alias_string,
                        dataset=dataset,
                    )
                    return BootstrapResult(
                        symbol=symbol,
                        outcome=BootstrapOutcome.UPSTREAM_ERROR,
                        registered=False,
                        backtest_data_available=False,
                        live_qualified=False,
                        diagnostics=f"malformed alias from Databento: {exc}",
                        dataset=dataset,
                    )

                return await self._upsert_and_classify(
                    symbol=symbol,
                    raw_symbol=raw_symbol_str,
                    alias_string=alias_string,
                    listing_venue=listing_venue,
                    derived_asset_class=derived_asset_class,
                    dataset=dataset,
                    venue_format="mic_code",
                    sm_factory=lambda session: SecurityMaster(
                        db=session, databento_client=self._databento
                    ),
                    normalize_unknown_exc=UnknownDatabentoVenueError,
                    normalize_fn=normalize_alias_for_registry,
                )

        return last_error or BootstrapResult(
            symbol=symbol,
            outcome=BootstrapOutcome.UPSTREAM_ERROR,
            registered=False,
            backtest_data_available=False,
            live_qualified=False,
            diagnostics=f"symbol not found in any entitled equity dataset: {_EQUITY_DATASETS}",
        )

    async def _upsert_and_classify(
        self,
        *,
        symbol: str,
        raw_symbol: str,
        alias_string: str,
        listing_venue: str,
        derived_asset_class: str,
        dataset: str,
        venue_format: str,
        sm_factory: Callable[[AsyncSession], Any],
        normalize_unknown_exc: type[Exception],
        normalize_fn: Callable[[str, str], str],
    ) -> BootstrapResult:
        """Run the write + classification under a single serialized transaction.

        Sequence:
        1. Open a session and acquire the same `pg_advisory_xact_lock`
           used by `_upsert_definition_and_alias` BEFORE the SELECT, so
           the pre-state we read is the one we'll also write against.
        2. SELECT the pre-upsert canonical alias (if any).
        3. Delegate to `_upsert_definition_and_alias` — the lock is
           already held (reentrant), so its internal acquire is a no-op.
        4. Classify CREATED / NOOP / ALIAS_ROTATED from the pre-state.
        5. Probe live-qualification.
        6. Commit — releases the advisory lock.
        """
        from sqlalchemy import text

        from msai.services.nautilus.security_master.service import compute_advisory_lock_key

        async with self._session_factory() as session:
            lock_key = compute_advisory_lock_key("databento", raw_symbol, derived_asset_class)
            try:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": lock_key},
                )
                existing_canonical = await self._find_active_databento_alias(
                    session, raw_symbol, derived_asset_class
                )

                sm = sm_factory(session)
                await sm._upsert_definition_and_alias(
                    raw_symbol=raw_symbol,
                    listing_venue=listing_venue,
                    routing_venue="SMART",
                    asset_class=derived_asset_class,
                    alias_string=alias_string,
                    provider="databento",
                    venue_format=venue_format,
                )
            except normalize_unknown_exc as exc:
                await session.rollback()
                log.warning(
                    "registry_bootstrap_unmapped_venue",
                    symbol=symbol,
                    alias_string=alias_string,
                    dataset=dataset,
                )
                return BootstrapResult(
                    symbol=symbol,
                    outcome=BootstrapOutcome.UNMAPPED_VENUE,
                    registered=False,
                    backtest_data_available=False,
                    live_qualified=False,
                    diagnostics=str(exc),
                    dataset=dataset,
                )
            except SQLAlchemyError as exc:
                await session.rollback()
                log.error(
                    "registry_bootstrap_db_error",
                    symbol=symbol,
                    alias_string=alias_string,
                    dataset=dataset,
                    exc_type=type(exc).__name__,
                    exc=str(exc),
                )
                return BootstrapResult(
                    symbol=symbol,
                    outcome=BootstrapOutcome.UPSTREAM_ERROR,
                    registered=False,
                    backtest_data_available=False,
                    live_qualified=False,
                    diagnostics=f"{type(exc).__name__}: {exc}",
                    dataset=dataset,
                )

            canonical_id = normalize_fn("databento", alias_string)

            if existing_canonical is None:
                outcome = BootstrapOutcome.CREATED
            elif existing_canonical == canonical_id:
                outcome = BootstrapOutcome.NOOP
            else:
                outcome = BootstrapOutcome.ALIAS_ROTATED

            live_qualified = await self._check_live_qualified(
                session, raw_symbol, derived_asset_class
            )
            await session.commit()

        return BootstrapResult(
            symbol=symbol,
            outcome=outcome,
            registered=True,
            backtest_data_available=None,
            live_qualified=live_qualified,
            canonical_id=canonical_id,
            dataset=dataset,
            asset_class=derived_asset_class,
        )

    async def _bootstrap_continuous_future(
        self,
        symbol: str,
        asset_class_override: str | None,
    ) -> BootstrapResult:
        """Continuous-contract resolution via
        ``SecurityMaster.resolve_for_backtest``.

        Typed Databento failures (401/429/5xx) are surfaced as their own
        outcomes so operator remediation advice matches the equity path.
        """
        from sqlalchemy import text

        from msai.services.data_sources.databento_client import AmbiguousDatabentoSymbolError
        from msai.services.data_sources.databento_errors import (
            DatabentoRateLimitedError,
            DatabentoUnauthorizedError,
            DatabentoUpstreamError,
        )
        from msai.services.nautilus.security_master.service import (
            SecurityMaster,
            compute_advisory_lock_key,
        )

        async with self._session_factory() as session:
            # Acquire the advisory lock BEFORE the pre-state SELECT — the
            # equity path does the same via _upsert_and_classify. Without
            # this, two concurrent bootstraps of the same continuous symbol
            # both observe existing_alias=None and both report CREATED even
            # though only one actually inserted. Data integrity is fine
            # (the lock inside _upsert_definition_and_alias still serializes
            # the write), but the operator-facing classification is wrong.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": compute_advisory_lock_key("databento", symbol, "futures")},
            )
            existing_alias = await self._find_active_databento_alias(session, symbol, "futures")

            sm = SecurityMaster(db=session, databento_client=self._databento)
            try:
                # Use a historical anchor (yesterday) so the upstream
                # symbol-existence probe hits a fully-published window;
                # mirrors the equity path's _7d/_1d window choice.
                resolved = await sm.resolve_for_backtest(
                    [symbol],
                    start=(date.today() - timedelta(days=1)).isoformat(),
                    end=None,
                    dataset=_FUTURES_DATASET,
                )
                await session.commit()
            except AmbiguousDatabentoSymbolError as exc:
                await session.rollback()
                return BootstrapResult(
                    symbol=symbol,
                    outcome=BootstrapOutcome.AMBIGUOUS,
                    registered=False,
                    backtest_data_available=False,
                    live_qualified=False,
                    candidates=exc.candidates,
                    dataset=_FUTURES_DATASET,
                )
            except DatabentoUnauthorizedError as exc:
                await session.rollback()
                log.warning(
                    "databento_bootstrap_dataset_unauthorized",
                    symbol=symbol,
                    dataset=_FUTURES_DATASET,
                )
                return BootstrapResult(
                    symbol=symbol,
                    outcome=BootstrapOutcome.UNAUTHORIZED,
                    registered=False,
                    backtest_data_available=False,
                    live_qualified=False,
                    diagnostics=str(exc),
                    dataset=_FUTURES_DATASET,
                )
            except DatabentoRateLimitedError as exc:
                await session.rollback()
                log.warning(
                    "databento_bootstrap_rate_limited",
                    symbol=symbol,
                    dataset=_FUTURES_DATASET,
                )
                return BootstrapResult(
                    symbol=symbol,
                    outcome=BootstrapOutcome.RATE_LIMITED,
                    registered=False,
                    backtest_data_available=False,
                    live_qualified=False,
                    diagnostics=str(exc),
                    dataset=_FUTURES_DATASET,
                )
            except DatabentoUpstreamError as exc:
                await session.rollback()
                log.warning(
                    "databento_bootstrap_dataset_upstream_error",
                    symbol=symbol,
                    dataset=_FUTURES_DATASET,
                    exc=str(exc),
                )
                return BootstrapResult(
                    symbol=symbol,
                    outcome=BootstrapOutcome.UPSTREAM_ERROR,
                    registered=False,
                    backtest_data_available=False,
                    live_qualified=False,
                    diagnostics=str(exc),
                    dataset=_FUTURES_DATASET,
                )
            except SQLAlchemyError as exc:
                await session.rollback()
                log.error(
                    "registry_bootstrap_db_error",
                    symbol=symbol,
                    dataset=_FUTURES_DATASET,
                    exc_type=type(exc).__name__,
                    exc=str(exc),
                )
                return BootstrapResult(
                    symbol=symbol,
                    outcome=BootstrapOutcome.UPSTREAM_ERROR,
                    registered=False,
                    backtest_data_available=False,
                    live_qualified=False,
                    diagnostics=f"{type(exc).__name__}: {exc}",
                    dataset=_FUTURES_DATASET,
                )

            new_canonical = resolved[0] if resolved else None
            if existing_alias is None:
                outcome = BootstrapOutcome.CREATED
            elif existing_alias == new_canonical:
                outcome = BootstrapOutcome.NOOP
            else:
                outcome = BootstrapOutcome.ALIAS_ROTATED

            live_qualified = await self._check_live_qualified(session, symbol, "futures")

        return BootstrapResult(
            symbol=symbol,
            outcome=outcome,
            registered=True,
            backtest_data_available=None,
            live_qualified=live_qualified,
            canonical_id=new_canonical,
            dataset=_FUTURES_DATASET,
            asset_class="futures",
        )

    @staticmethod
    async def _find_active_databento_alias(
        session: AsyncSession, raw_symbol: str, asset_class: str
    ) -> str | None:
        """Single-row variant used by the continuous-futures branch."""
        from sqlalchemy import select

        from msai.models.instrument_alias import InstrumentAlias
        from msai.models.instrument_definition import InstrumentDefinition

        stmt = (
            select(InstrumentAlias.alias_string)
            .join(
                InstrumentDefinition,
                InstrumentDefinition.instrument_uid == InstrumentAlias.instrument_uid,
            )
            .where(
                InstrumentDefinition.raw_symbol == raw_symbol,
                InstrumentDefinition.asset_class == asset_class,
                InstrumentAlias.provider == "databento",
                InstrumentAlias.effective_to.is_(None),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def _check_live_qualified(
        session: AsyncSession, raw_symbol: str, asset_class: str
    ) -> bool:
        """True iff an active interactive_brokers alias exists for this
        ``(raw_symbol, asset_class)`` pair.

        Filtering on ``asset_class`` is essential for cross-asset-class
        symbol collisions: a futures ``ES`` IB alias must not flag an
        equity ``ES`` bootstrap as live-qualified.
        """
        from sqlalchemy import select

        from msai.models.instrument_alias import InstrumentAlias
        from msai.models.instrument_definition import InstrumentDefinition

        stmt = (
            select(InstrumentAlias.id)
            .join(
                InstrumentDefinition,
                InstrumentDefinition.instrument_uid == InstrumentAlias.instrument_uid,
            )
            .where(
                InstrumentDefinition.raw_symbol == raw_symbol,
                InstrumentDefinition.asset_class == asset_class,
                InstrumentAlias.provider == "interactive_brokers",
                InstrumentAlias.effective_to.is_(None),
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None
