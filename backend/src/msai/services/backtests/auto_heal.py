"""Auto-heal orchestrator (Task B7).

Runs one bounded cycle: derive asset_class, check guardrails, dedupe-lock
+ enqueue the ingest job, poll to completion, re-verify catalog coverage.
Called from :func:`msai.workers.backtest_job.run_backtest_job`'s
retry-once branch. See
``docs/plans/2026-04-21-backtest-auto-ingest-on-missing-data.md`` and
``docs/prds/backtest-auto-ingest-on-missing-data.md`` for the full
design.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog
from arq.jobs import Job, JobStatus
from sqlalchemy import update

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import enqueue_ingest
from msai.models.backtest import Backtest
from msai.services.backtests.auto_heal_guardrails import evaluate_guardrails
from msai.services.backtests.auto_heal_lock import (
    AutoHealLock,
    build_lock_key,
)
from msai.services.backtests.derive_asset_class import derive_asset_class

if TYPE_CHECKING:
    from datetime import date
    from pathlib import Path

    from arq.connections import ArqRedis


__all__ = ["AutoHealOutcome", "AutoHealResult", "run_auto_heal"]


log = get_logger(__name__)


class AutoHealOutcome(StrEnum):
    """Terminal outcome of a single ``run_auto_heal`` cycle."""

    SUCCESS = "success"
    GUARDRAIL_REJECTED = "guardrail_rejected"
    TIMEOUT = "timeout"
    INGEST_FAILED = "ingest_failed"
    COVERAGE_STILL_MISSING = "coverage_still_missing"


@dataclass(frozen=True, slots=True)
class AutoHealResult:
    """Outcome + diagnostics of a single auto-heal cycle."""

    outcome: AutoHealOutcome
    asset_class: str
    resolved_instrument_ids: list[str] | None
    reason_human: str | None
    gaps: list[tuple[str, list[tuple[int, int]]]] | None = None

    def __post_init__(self) -> None:
        """Enforce outcome-dependent field invariants.

        See the ``ValueError`` messages below for the contract.
        """
        if self.outcome != AutoHealOutcome.COVERAGE_STILL_MISSING and self.gaps is not None:
            raise ValueError(f"gaps must be None for outcome={self.outcome}")
        if self.outcome == AutoHealOutcome.SUCCESS and self.reason_human is not None:
            raise ValueError("SUCCESS must have reason_human=None")
        if self.outcome != AutoHealOutcome.SUCCESS and self.reason_human is None:
            raise ValueError(f"outcome={self.outcome} requires reason_human to be set")


async def run_auto_heal(
    *,
    backtest_id: str,
    instruments: list[str],
    start: date,
    end: date,
    catalog_root: Path,
    caller_asset_class_hint: str | None,
    pool: ArqRedis,
) -> AutoHealResult:
    """Execute one bounded auto-heal cycle.

    All arguments are required kwargs — the caller is
    ``msai.workers.backtest_job`` which owns the arq ``ctx`` (source of
    ``pool = ctx["redis"]``) and the backtest snapshot.
    """
    structlog.contextvars.bind_contextvars(backtest_id=backtest_id)
    try:
        # ---------------------------------------------------------------
        # 1. Async asset-class derivation (registry → shape → hint → default)
        # ---------------------------------------------------------------
        async with async_session_factory() as db:
            derived = await derive_asset_class(instruments, start=start, db=db)
        asset_class = derived or caller_asset_class_hint or "stocks"

        log.info(
            "backtest_auto_heal_started",
            symbols=instruments,
            asset_class=asset_class,
            start=start.isoformat(),
            end=end.isoformat(),
        )

        # ---------------------------------------------------------------
        # 2. Guardrails — council-locked caps
        # ---------------------------------------------------------------
        guardrails = evaluate_guardrails(
            asset_class=asset_class,
            symbols=instruments,
            start=start,
            end=end,
            max_years=settings.auto_heal_max_years,
            max_symbols=settings.auto_heal_max_symbols,
            allow_options=settings.auto_heal_allow_options,
        )
        if not guardrails.allowed:
            log.info(
                "backtest_auto_heal_guardrail_rejected",
                reason=guardrails.reason,
                details=guardrails.details,
            )
            return AutoHealResult(
                outcome=AutoHealOutcome.GUARDRAIL_REJECTED,
                asset_class=asset_class,
                resolved_instrument_ids=None,
                reason_human=guardrails.human_message,
            )

        # ---------------------------------------------------------------
        # 3. Dedupe lock + enqueue (or wait for existing job)
        # ---------------------------------------------------------------
        lock = AutoHealLock(pool)
        lock_key = build_lock_key(
            asset_class=asset_class,
            symbols=instruments,
            start=start,
            end=end,
        )

        ingest_job_id: str | None = None
        # Unique placeholder per caller so the Lua CAS can verify
        # ownership before swapping in the real job_id.
        placeholder = f"reserving:{backtest_id}:{uuid4().hex[:8]}"
        acquired = await lock.try_acquire(
            lock_key,
            ttl_s=settings.auto_heal_lock_ttl_seconds,
            holder_id=placeholder,
        )
        try:
            if acquired:
                job = await enqueue_ingest(
                    pool=pool,
                    asset_class=asset_class,
                    symbols=instruments,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
                if job is None:
                    log.warning("backtest_auto_heal_ingest_enqueue_declined")
                    # Release placeholder so the next caller gets a fresh slot.
                    await lock.release(lock_key, holder_id=placeholder)
                    return AutoHealResult(
                        outcome=AutoHealOutcome.INGEST_FAILED,
                        asset_class=asset_class,
                        resolved_instrument_ids=None,
                        reason_human="Ingest queue declined the job (unlikely).",
                    )
                ingest_job_id = job.job_id

                # CAS placeholder → real job_id; if the placeholder TTL expired
                # mid-enqueue and someone else grabbed the lock we must NOT
                # trample their value. The arq job still runs either way;
                # catalog coverage is the real gate.
                swap_ok = await lock.compare_and_swap(
                    lock_key,
                    from_holder=placeholder,
                    to_holder=ingest_job_id,
                    ttl_s=settings.auto_heal_lock_ttl_seconds,
                )
                if not swap_ok:
                    log.warning(
                        "auto_heal_lock_cas_lost",
                        lock_key=lock_key,
                        ingest_job_id=ingest_job_id,
                    )
                dedupe_result = "acquired"
            else:
                existing = await lock.get_holder(lock_key)
                if existing and not existing.startswith("reserving:"):
                    ingest_job_id = existing
                else:
                    # Acquiring holder is still in the placeholder window;
                    # brief wait for them to swap in the real job id.
                    await asyncio.sleep(2)
                    existing = await lock.get_holder(lock_key)
                    ingest_job_id = (
                        existing if existing and not existing.startswith("reserving:") else None
                    )
                dedupe_result = (
                    f"wait_for_existing:{ingest_job_id}"
                    if ingest_job_id
                    else "wait_race_placeholder_lost"
                )

            log.info(
                "backtest_auto_heal_ingest_enqueued",
                ingest_job_id=ingest_job_id,
                lock_key=lock_key,
                dedupe_result=dedupe_result,
            )

            await _set_backtest_phase(
                backtest_id=backtest_id,
                phase="awaiting_data",
                progress_message=(
                    f"Downloading {asset_class} data for "
                    + ",".join(instruments[:3])
                    + ("..." if len(instruments) > 3 else "")
                ),
                heal_started_at=datetime.now(UTC),
                heal_job_id=ingest_job_id,
            )

            if ingest_job_id is None:
                return AutoHealResult(
                    outcome=AutoHealOutcome.INGEST_FAILED,
                    asset_class=asset_class,
                    resolved_instrument_ids=None,
                    reason_human="Could not determine ingest job id after dedupe race.",
                )

            # ---------------------------------------------------------------
            # 4. Poll arq Job status with wall-clock cap
            # ---------------------------------------------------------------
            ingest_job = Job(
                ingest_job_id,
                redis=pool,
                _queue_name=settings.ingest_queue_name,
            )
            cap = settings.auto_heal_wall_clock_cap_seconds
            interval = settings.auto_heal_poll_interval_seconds
            deadline = time.monotonic() + cap
            ingest_start = time.monotonic()
            poll_outcome: str | None = None  # "complete" | "not_found"

            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                status = await ingest_job.status()
                if status == JobStatus.complete:
                    # Fetch result to detect worker-side exception.
                    try:
                        await ingest_job.result(timeout=5.0)
                    except Exception:  # noqa: BLE001 — ingest worker failure
                        log.exception(
                            "backtest_auto_heal_ingest_failed",
                            ingest_job_id=ingest_job_id,
                        )
                        return AutoHealResult(
                            outcome=AutoHealOutcome.INGEST_FAILED,
                            asset_class=asset_class,
                            resolved_instrument_ids=None,
                            reason_human=("Ingest provider returned an error; see worker logs."),
                        )
                    poll_outcome = "complete"
                    break
                if status == JobStatus.not_found:
                    # arq retention ejected the result before we polled.
                    # The catalog is the source of truth; skip result()
                    # (would raise ResultNotFound) and go straight to
                    # coverage re-check.
                    log.info(
                        "backtest_auto_heal_ingest_status_not_found_falling_through",
                        ingest_job_id=ingest_job_id,
                    )
                    poll_outcome = "not_found"
                    break

            if poll_outcome is None:
                log.warning(
                    "backtest_auto_heal_timeout",
                    wall_clock_seconds=cap,
                    ingest_job_id=ingest_job_id,
                )
                return AutoHealResult(
                    outcome=AutoHealOutcome.TIMEOUT,
                    asset_class=asset_class,
                    resolved_instrument_ids=None,
                    reason_human=f"Data download exceeded {cap // 60}-minute cap.",
                )

            if poll_outcome == "complete":
                log.info(
                    "backtest_auto_heal_ingest_completed",
                    ingest_duration_seconds=int(time.monotonic() - ingest_start),
                )

            # ---------------------------------------------------------------
            # 5. Coverage re-check
            # ---------------------------------------------------------------
            # Imports are deferred until after the poll loop so a coverage
            # check failure in a remote context doesn't mask the earlier
            # orchestrator error paths.
            # Resolve to canonical IDs the catalog actually stores. The catalog
            # builder writes under e.g. SPY.NASDAQ (Nautilus venue convention),
            # while SecurityMaster.resolve_for_backtest returns SPY.XNAS (MIC
            # code). Those diverge. Use ensure_catalog_data here — same helper
            # the backtest subprocess uses — so coverage verification looks in
            # the same directories the subprocess will read from.
            from msai.core.config import settings as _settings
            from msai.services.nautilus.catalog_builder import (
                ensure_catalog_data,
                verify_catalog_coverage,
            )

            try:
                resolved_ids = ensure_catalog_data(
                    symbols=instruments,
                    raw_parquet_root=_settings.parquet_root,
                    catalog_root=catalog_root,
                    asset_class=asset_class,
                )
            except Exception:  # noqa: BLE001 — catalog rebuild can still fail after a partial ingest
                log.warning(
                    "auto_heal_canonical_resolution_failed",
                    exc_info=True,
                )
                resolved_ids = list(instruments)

            try:
                gaps = verify_catalog_coverage(
                    catalog_root=catalog_root,
                    instrument_ids=resolved_ids,
                    start=start,
                    end=end,
                )
            except Exception:  # noqa: BLE001 — coverage check failure
                log.exception(
                    "backtest_auto_heal_coverage_check_failed",
                    backtest_id=backtest_id,
                )
                return AutoHealResult(
                    outcome=AutoHealOutcome.COVERAGE_STILL_MISSING,
                    asset_class=asset_class,
                    resolved_instrument_ids=resolved_ids,
                    reason_human=(
                        "Post-ingest coverage verification failed (catalog API error); "
                        "see server logs. Retry the backtest to re-verify."
                    ),
                    gaps=None,  # None = verification errored, vs [] = zero gaps (not used)
                )
            # Tolerate small edge gaps (market-closed days at either end of
            # the requested range — e.g., New Year's Day, weekends, pre/post
            # market hours on the boundary days). Nautilus's
            # get_missing_intervals_for_request does strict contiguous
            # coverage in nanoseconds, which flags these legitimate
            # equity/futures market-hour gaps as "missing."
            #
            # Threshold: any instrument with more than
            # COVERAGE_TOLERANCE_DAYS of total gap time is flagged as
            # COVERAGE_STILL_MISSING. This catches real partial returns
            # ("Jun-Dec when Jan-Dec requested" = ~150-day gap) while
            # accepting the holiday/weekend tails that naturally occur at
            # month/year boundaries. Tuned per asset class would be
            # ideal (equities have ~2/7 of calendar time in
            # weekends + holidays) but that's a follow-up; 7 days handles
            # the realistic worst case for month-spanning requests.
            _COVERAGE_TOLERANCE_NS = 7 * 24 * 3600 * 1_000_000_000
            significant_gaps: list[tuple[str, list[tuple[int, int]]]] = []
            for inst_id, inst_gaps in gaps:
                total_gap_ns = sum(end_ns - start_ns for start_ns, end_ns in inst_gaps)
                if total_gap_ns > _COVERAGE_TOLERANCE_NS:
                    significant_gaps.append((inst_id, inst_gaps))
            if significant_gaps:
                log.warning(
                    "backtest_auto_heal_coverage_still_missing",
                    gaps=[
                        {"instrument_id": iid, "gap_count": len(g)} for iid, g in significant_gaps
                    ],
                )
                return AutoHealResult(
                    outcome=AutoHealOutcome.COVERAGE_STILL_MISSING,
                    asset_class=asset_class,
                    resolved_instrument_ids=resolved_ids,
                    reason_human=("Provider returned a narrower range than requested."),
                    gaps=significant_gaps,
                )

            log.info("backtest_auto_heal_completed", outcome="success")
            return AutoHealResult(
                outcome=AutoHealOutcome.SUCCESS,
                asset_class=asset_class,
                resolved_instrument_ids=resolved_ids,
                reason_human=None,
            )

        finally:
            # Release lock only if WE acquired it — never steal. The
            # current value may be our placeholder, our ingest_job_id,
            # or (after CAS-loss) a stranger's token. Release only in
            # the first two cases.
            if acquired:
                current = await lock.get_holder(lock_key)
                if current is not None and current in (placeholder, ingest_job_id):
                    await lock.release(lock_key, holder_id=current)
            await _set_backtest_phase(
                backtest_id=backtest_id,
                phase=None,
                progress_message=None,
                heal_started_at=None,
                heal_job_id=None,
            )
    finally:
        structlog.contextvars.unbind_contextvars("backtest_id")


async def _set_backtest_phase(
    *,
    backtest_id: str,
    phase: str | None,
    progress_message: str | None,
    heal_started_at: datetime | None,
    heal_job_id: str | None,
) -> None:
    """Atomically update the 4 auto-heal lifecycle columns.

    On terminal transition (``phase=None``), also clears
    ``heal_started_at`` and ``heal_job_id`` so the row reflects
    post-heal state cleanly.
    """
    values: dict[str, Any] = {
        "phase": phase,
        "progress_message": progress_message,
    }
    if phase is None:
        # Terminal — clear all auto-heal lifecycle fields.
        values["heal_started_at"] = None
        values["heal_job_id"] = None
    else:
        if heal_started_at is not None:
            values["heal_started_at"] = heal_started_at
        if heal_job_id is not None:
            values["heal_job_id"] = heal_job_id
    try:
        async with async_session_factory() as session:
            await session.execute(
                update(Backtest).where(Backtest.id == backtest_id).values(**values)
            )
            await session.commit()
    except Exception:
        log.exception(
            "backtest_auto_heal_phase_update_failed",
            backtest_id=backtest_id,
        )
