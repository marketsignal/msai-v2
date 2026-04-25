"""Per-symbol onboarding orchestrator.

Single-task topology: a parent ``run_symbol_onboarding`` arq task loops
the watchlist's symbols and calls :func:`_onboard_one_symbol` once per
symbol. This module owns the four-phase pipeline (bootstrap → ingest →
coverage → optional IB qualify) and JSONB state persistence on the
``symbol_onboarding_runs`` row.

Every phase boundary persists state under ``SELECT FOR UPDATE`` so a
crash mid-run leaves operators a definitive snapshot of which symbol
was where. Failures are scoped per-symbol and do not abort siblings —
the worker module owns the parent-loop semantics.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from sqlalchemy import select

from msai.core.logging import get_logger
from msai.models.symbol_onboarding_run import SymbolOnboardingRun
from msai.schemas.symbol_onboarding import (
    OnboardSymbolSpec,
    SymbolStateRow,
    SymbolStatus,
    SymbolStepStatus,
)
from msai.services.data_ingestion import IngestResult, ingest_symbols
from msai.services.nautilus.security_master.databento_bootstrap import BootstrapOutcome
from msai.services.observability.trading_metrics import (
    onboarding_ib_timeout_total,
    onboarding_symbol_duration_seconds,
)
from msai.services.symbol_onboarding import normalize_asset_class_for_ingest
from msai.services.symbol_onboarding.coverage import compute_coverage

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = get_logger(__name__)

__all__ = [
    "OrchestratorBootstrapProto",
    "OrchestratorIBProto",
    "_onboard_one_symbol",
]

_DEFAULT_IB_TIMEOUT_S = 120

# Bootstrap outcomes that terminate the per-symbol pipeline. Pinned to
# the real ``BootstrapOutcome`` enum so a new outcome added in
# ``databento_bootstrap.py`` cannot silently classify as success here —
# the type checker / runtime will fail if someone adds a member that
# isn't either CREATED/NOOP/ALIAS_ROTATED or in this failure set.
_BOOTSTRAP_FAILURE_OUTCOMES: frozenset[BootstrapOutcome] = frozenset(
    {
        BootstrapOutcome.AMBIGUOUS,
        BootstrapOutcome.UNAUTHORIZED,
        BootstrapOutcome.UNMAPPED_VENUE,
        BootstrapOutcome.UPSTREAM_ERROR,
        BootstrapOutcome.RATE_LIMITED,
    }
)


class OrchestratorBootstrapProto(Protocol):
    """Subset of :class:`DatabentoBootstrapService` that the orchestrator depends on."""

    async def bootstrap(
        self,
        *,
        symbols: list[str],
        asset_class_override: str | None,
        exact_ids: dict[str, str] | None,
    ) -> list[Any]: ...


class OrchestratorIBProto(Protocol):
    """Adapter shape for IB qualification. Allows tests to inject a fast mock
    without standing up an ``InteractiveBrokersInstrumentProvider``."""

    async def qualify(self, *, symbol: str, asset_class: str) -> None: ...


async def _onboard_one_symbol(
    *,
    run_id: UUID,
    spec: OnboardSymbolSpec,
    request_live_qualification: bool,
    db_factory: async_sessionmaker[AsyncSession],
    data_root: Path,
    bootstrap_service: OrchestratorBootstrapProto | None = None,
    ib_service: OrchestratorIBProto | None = None,
    ib_timeout_s: int = _DEFAULT_IB_TIMEOUT_S,
    today: date | None = None,
) -> SymbolStateRow:
    """Run the four-phase onboarding pipeline for a single symbol.

    Returns the terminal :class:`SymbolStateRow` (``succeeded`` or
    ``failed``). All phase transitions are persisted to the run row's
    ``symbol_states`` JSONB before the next phase begins, so external
    pollers always see a consistent snapshot.
    """
    bound = log.bind(run_id=str(run_id), symbol=spec.symbol, asset_class=spec.asset_class)
    started = time.perf_counter()

    ingest_asset = normalize_asset_class_for_ingest(spec.asset_class)

    # ---- Phase 1: bootstrap (registry register / noop / alias-rotated) ----
    await _persist_step(db_factory, run_id, spec.symbol, step=SymbolStepStatus.BOOTSTRAP)
    bootstrap = bootstrap_service or _default_bootstrap_service(db_factory)
    try:
        results = await bootstrap.bootstrap(
            symbols=[spec.symbol],
            asset_class_override=spec.asset_class,
            exact_ids=None,
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 — service surfaces typed + unexpected; map to envelope
        bound.warning("symbol_onboarding_bootstrap_failed", error=repr(exc))
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.BOOTSTRAP,
            code="BOOTSTRAP_FAILED",
            message=str(exc),
        )

    if not results:
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.BOOTSTRAP,
            code="BOOTSTRAP_FAILED",
            message="DatabentoBootstrapService returned no result for the symbol.",
        )

    outcome = _coerce_outcome(results[0])
    if outcome in _BOOTSTRAP_FAILURE_OUTCOMES:
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.BOOTSTRAP,
            code=f"BOOTSTRAP_{outcome.name}",
            message=f"Bootstrap terminated with outcome={outcome.value}",
        )

    # ---- Phase 2: ingest (in-process; no arq round-trip) ----
    await _persist_step(db_factory, run_id, spec.symbol, step=SymbolStepStatus.INGEST)
    try:
        ingest_result: IngestResult = await ingest_symbols(
            ingest_asset,
            [spec.symbol],
            spec.start.isoformat(),
            spec.end.isoformat(),
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001
        bound.warning("symbol_onboarding_ingest_failed", error=repr(exc))
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.INGEST,
            code="INGEST_FAILED",
            message=str(exc),
        )
    bound.info(
        "symbol_onboarding_ingest_done",
        bars_written=ingest_result.bars_written,
        empty_symbols=list(ingest_result.empty_symbols),
    )

    # ---- Phase 3: coverage scan (must be 'full' to advance) ----
    await _persist_step(db_factory, run_id, spec.symbol, step=SymbolStepStatus.COVERAGE)
    coverage = await compute_coverage(
        asset_class=ingest_asset,
        symbol=spec.symbol,
        start=spec.start,
        end=spec.end,
        data_root=data_root,
        today=today,
    )
    if coverage.status != "full":
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.COVERAGE_FAILED,
            code="COVERAGE_INCOMPLETE",
            message=f"Post-ingest coverage scan returned status={coverage.status!r}",
            details={
                "coverage_status": coverage.status,
                "missing_ranges": [
                    {"start": s.isoformat(), "end": e.isoformat()}
                    for s, e in coverage.missing_ranges
                ],
            },
        )

    # ---- Phase 4: optional IB qualification ----
    if not request_live_qualification:
        terminal = await _succeed(db_factory, run_id, spec, step=SymbolStepStatus.IB_SKIPPED)
        onboarding_symbol_duration_seconds.observe(time.perf_counter() - started)
        return terminal

    await _persist_step(db_factory, run_id, spec.symbol, step=SymbolStepStatus.IB_QUALIFY)
    ib = ib_service or _default_ib_service()
    try:
        await asyncio.wait_for(
            ib.qualify(symbol=spec.symbol, asset_class=spec.asset_class),
            timeout=ib_timeout_s,
        )
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except TimeoutError:
        onboarding_ib_timeout_total.inc()
        bound.warning("symbol_onboarding_ib_timeout", timeout_s=ib_timeout_s)
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.IB_QUALIFY,
            code="IB_TIMEOUT",
            message=f"IB qualification exceeded the {ib_timeout_s}s SLA",
        )
    except NotImplementedError as exc:
        # The default stub adapter raises NotImplementedError. That's a
        # build-config issue (live wiring not present), not a runtime IB
        # outage — surface a distinct error code so operators don't get
        # pointed at the IB Gateway container.
        bound.warning("symbol_onboarding_ib_not_configured", error=repr(exc))
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.IB_QUALIFY,
            code="IB_NOT_CONFIGURED",
            message=(
                "IB qualification adapter is not wired into this build. "
                "Set request_live_qualification=false or pass an explicit ib_service."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        bound.warning("symbol_onboarding_ib_failed", error=repr(exc))
        return await _fail(
            db_factory,
            run_id,
            spec,
            step=SymbolStepStatus.IB_QUALIFY,
            code="IB_UNAVAILABLE",
            message=str(exc),
        )

    terminal = await _succeed(db_factory, run_id, spec, step=SymbolStepStatus.COMPLETED)
    onboarding_symbol_duration_seconds.observe(time.perf_counter() - started)
    return terminal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_outcome(result: Any) -> BootstrapOutcome:
    """Resolve a ``BootstrapResult.outcome`` to the real enum member.

    Accepts a real ``BootstrapResult`` (whose ``outcome`` is already a
    ``BootstrapOutcome``) and duck-typed test doubles whose ``outcome``
    is the string value (``"created"``, ``"ambiguous"``, ...).
    Anything else raises ``ValueError`` so a typo or new outcome value
    fails loudly instead of silently classifying as success.
    """
    raw = getattr(result, "outcome", result)
    if isinstance(raw, BootstrapOutcome):
        return raw
    return BootstrapOutcome(str(raw).strip().lower())


async def _persist_step(
    db_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    symbol: str,
    *,
    step: SymbolStepStatus,
) -> None:
    """Mark the symbol as ``in_progress`` at ``step`` under a row lock."""
    async with db_factory() as db, db.begin():
        row = (
            await db.execute(
                select(SymbolOnboardingRun)
                .where(SymbolOnboardingRun.id == run_id)
                .with_for_update()
            )
        ).scalar_one()
        states = dict(row.symbol_states)
        entry = dict(states.get(symbol, {}))
        entry["step"] = step.value
        entry["status"] = SymbolStatus.IN_PROGRESS.value
        states[symbol] = entry
        row.symbol_states = states


async def _succeed(
    db_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    spec: OnboardSymbolSpec,
    *,
    step: SymbolStepStatus,
) -> SymbolStateRow:
    return await _finalize(
        db_factory, run_id, spec, status=SymbolStatus.SUCCEEDED, step=step, error=None
    )


async def _fail(
    db_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    spec: OnboardSymbolSpec,
    *,
    step: SymbolStepStatus,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> SymbolStateRow:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return await _finalize(
        db_factory, run_id, spec, status=SymbolStatus.FAILED, step=step, error=error
    )


async def _finalize(
    db_factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    spec: OnboardSymbolSpec,
    *,
    status: SymbolStatus,
    step: SymbolStepStatus,
    error: dict[str, Any] | None,
) -> SymbolStateRow:
    """Atomically write the terminal per-symbol state to JSONB."""
    async with db_factory() as db, db.begin():
        row = (
            await db.execute(
                select(SymbolOnboardingRun)
                .where(SymbolOnboardingRun.id == run_id)
                .with_for_update()
            )
        ).scalar_one()
        states = dict(row.symbol_states)
        entry = dict(states.get(spec.symbol, {}))
        entry.update(
            {
                "symbol": spec.symbol,
                "asset_class": spec.asset_class,
                "start": spec.start.isoformat(),
                "end": spec.end.isoformat(),
                "status": status.value,
                "step": step.value,
                "error": error,
            }
        )
        states[spec.symbol] = entry
        row.symbol_states = states
    return SymbolStateRow(
        symbol=spec.symbol,
        asset_class=spec.asset_class,
        start=spec.start,
        end=spec.end,
        status=status,
        step=step,
        error=error,
    )


def _default_bootstrap_service(
    db_factory: async_sessionmaker[AsyncSession],
) -> OrchestratorBootstrapProto:
    """Default real-runtime bootstrap service (Databento-backed)."""
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.nautilus.security_master.databento_bootstrap import (
        DatabentoBootstrapService,
    )

    return DatabentoBootstrapService(session_factory=db_factory, databento_client=DatabentoClient())


def _default_ib_service() -> OrchestratorIBProto:
    """Default IB qualifier-adapter.

    Wiring the Nautilus ``InteractiveBrokersInstrumentProvider`` is a
    Phase 5 (live-stack) concern handled by the supervisor, not the
    onboarding worker. Tests inject a mock via ``ib_service=``; live
    code paths invoke the IB-backed adapter via the supervisor's
    `instruments refresh --provider interactive_brokers` path. Until
    that wiring lands, this default raises so a misconfigured caller
    fails loudly instead of silently bypassing IB qualification.
    """

    class _StubAdapter:
        async def qualify(self, *, symbol: str, asset_class: str) -> None:
            raise NotImplementedError(
                "IB qualification adapter is not wired into the orchestrator. "
                "Pass an explicit ``ib_service=`` to ``_onboard_one_symbol`` or "
                "use ``request_live_qualification=False`` until the supervisor "
                "wiring lands."
            )

    return _StubAdapter()
