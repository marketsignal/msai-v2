"""arq worker task: ``run_symbol_onboarding``.

Single-task topology: the parent task loops the run's symbols sequentially
and delegates the per-symbol four-phase pipeline to
:func:`msai.services.symbol_onboarding.orchestrator._onboard_one_symbol`.

Run-level status semantics:

* ``COMPLETED`` — every symbol terminal-state ``succeeded``.
* ``COMPLETED_WITH_FAILURES`` — at least one symbol terminal-state ``failed``;
  per-symbol errors are persisted in the row's ``symbol_states`` JSONB.
* ``FAILED`` — reserved for systemic short-circuits (the outer try/except
  catches an unhandled exception). Per-symbol failures NEVER bubble to
  run-level ``failed``.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from arq import Retry
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.schemas.symbol_onboarding import (
    OnboardSymbolSpec,
    SymbolStateRow,
    SymbolStatus,
)
from msai.services.observability.trading_metrics import (
    onboarding_jobs_total,
)
from msai.services.symbol_onboarding.orchestrator import _onboard_one_symbol

log = get_logger(__name__)

__all__ = ["run_symbol_onboarding"]


async def run_symbol_onboarding(ctx: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """Parent arq task: orchestrates per-symbol onboarding for a run.

    The single context kwarg is ``run_id`` -- the UUID string of an existing
    ``SymbolOnboardingRun`` row. The row must already be persisted by the
    POST /onboard handler before enqueue. We do NOT create the row here.
    """
    uid = UUID(run_id)
    bound = log.bind(run_id=str(uid))
    bound.info("symbol_onboarding_worker_started")

    try:
        # ---- Phase A: flip PENDING -> IN_PROGRESS, snapshot symbol specs ----
        async with async_session_factory() as db, db.begin():
            row = (
                await db.execute(
                    select(SymbolOnboardingRun)
                    .where(SymbolOnboardingRun.id == uid)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                # Race window: API enqueues the arq job BEFORE committing the
                # row (council-pinned ordering). Worker can pick up the job
                # before the row is visible — requeue with backoff so the
                # transactional write window has time to commit.
                bound.warning("symbol_onboarding_worker_run_missing_requeue")
                raise Retry(defer=2)
            row.status = SymbolOnboardingRunStatus.IN_PROGRESS
            row.started_at = datetime.now(UTC)
            specs = _hydrate_specs(row.symbol_states)
            request_live_qualification = row.request_live_qualification

        # ---- Phase B: per-symbol pipeline ----
        per_symbol_states: list[SymbolStateRow] = []
        for spec in specs:
            t0 = time.monotonic()
            state = await _onboard_one_symbol(
                run_id=uid,
                spec=spec,
                request_live_qualification=request_live_qualification,
                db_factory=async_session_factory,
                data_root=Path(settings.data_root),
            )
            elapsed = time.monotonic() - t0
            # NOTE: ``onboarding_symbol_duration_seconds`` is observed by the
            # orchestrator on its terminal paths — observing again here would
            # double-count successful symbols. Keep the wall-clock measurement
            # for structured logs only; the orchestrator owns the metric.
            bound.info(
                "symbol_onboarding_step_completed",
                symbol=spec.symbol,
                terminal_step=state.step,
                terminal_status=state.status,
                elapsed_s=round(elapsed, 3),
            )
            per_symbol_states.append(state)

        # ---- Phase C: terminal status sync ----
        terminal = _compute_terminal_status(per_symbol_states)
        async with async_session_factory() as db, db.begin():
            row = (
                await db.execute(
                    select(SymbolOnboardingRun)
                    .where(SymbolOnboardingRun.id == uid)
                    .with_for_update()
                )
            ).scalar_one()
            row.status = terminal
            row.completed_at = datetime.now(UTC)

        onboarding_jobs_total.labels(status=terminal.value).inc()
        bound.info(
            "symbol_onboarding_worker_completed",
            terminal_status=terminal.value,
            symbol_count=len(per_symbol_states),
        )
        return {"status": terminal.value, "run_id": str(uid)}

    except Retry:
        # arq retry signal — not a failure. Let arq see it untouched so the
        # job is requeued with the configured backoff.
        raise
    except Exception as exc:
        # Systemic short-circuit: best-effort sync run row to FAILED before
        # re-raising so arq's retry/DLQ machinery sees the original exception.
        bound.exception("symbol_onboarding_worker_crashed", error=repr(exc))
        try:
            async with async_session_factory() as db, db.begin():
                row = (
                    await db.execute(
                        select(SymbolOnboardingRun)
                        .where(SymbolOnboardingRun.id == uid)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.status = SymbolOnboardingRunStatus.FAILED
                    row.completed_at = datetime.now(UTC)
        except (SQLAlchemyError, OperationalError):
            # Narrow recovery: only swallow DB-side failures so DB outages
            # don't mask the original crash. Programmer errors (TypeError,
            # AttributeError) re-raise and surface in arq's DLQ.
            bound.exception("symbol_onboarding_worker_status_sync_failed")
        try:
            onboarding_jobs_total.labels(status=SymbolOnboardingRunStatus.FAILED.value).inc()
        except Exception:  # noqa: BLE001 — metrics are best-effort
            bound.error(
                "symbol_onboarding_worker_metric_emit_failed",
                metric_name="onboarding_jobs_total",
                exc_info=True,
            )
        raise


def _hydrate_specs(symbol_states: dict[str, Any]) -> list[OnboardSymbolSpec]:
    """Reconstruct ``OnboardSymbolSpec`` list from the row's JSONB snapshot."""
    specs: list[OnboardSymbolSpec] = []
    for entry in symbol_states.values():
        specs.append(
            OnboardSymbolSpec(
                symbol=entry["symbol"],
                asset_class=entry["asset_class"],
                start=date.fromisoformat(entry["start"]),
                end=date.fromisoformat(entry["end"]),
            )
        )
    return specs


def _compute_terminal_status(
    per_symbol: list[SymbolStateRow],
) -> SymbolOnboardingRunStatus:
    """Map per-symbol terminal states to a run-level terminal status.

    Per-symbol failures NEVER bubble to run-level ``FAILED``. ``FAILED`` is
    reserved for the outer try/except path (systemic short-circuit).
    All-success -> ``COMPLETED``; everything else -> ``COMPLETED_WITH_FAILURES``.
    """
    if all(s.status == SymbolStatus.SUCCEEDED for s in per_symbol):
        return SymbolOnboardingRunStatus.COMPLETED
    return SymbolOnboardingRunStatus.COMPLETED_WITH_FAILURES
