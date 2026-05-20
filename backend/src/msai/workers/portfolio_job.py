"""arq worker function for portfolio-level backtest runs.

Lifecycle for a single job:

1. Mark the :class:`PortfolioRun` row ``running`` and stamp the heartbeat.
   Terminal rows (``completed`` / ``failed``) are short-circuited to
   prevent arq retries from re-executing finished work.
2. Acquire compute slots sized by the run's ``max_parallelism`` and
   start a background task that renews the Redis lease so it cannot
   expire during long backtests.
3. Hand off to :meth:`PortfolioService.run_portfolio_backtest` which
   runs each allocation's backtest, combines the weighted returns, and
   persists status/metrics/series/allocations/report_path.
4. On a data-shape failure (``PortfolioOrchestrationError``) mark the
   run ``failed`` and **do not** re-raise — arq's retry semantics would
   pick the row back up and the terminal-state guard in
   :meth:`mark_run_running` would then refuse the retry (leaving the
   arq job in a confused state).  Re-raise only on infrastructure
   errors so arq can dead-letter / retry them.
5. Always stop the renewal task and release compute slots in the
   ``finally`` block.
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import cast, func, update
from sqlalchemy.dialects.postgresql import JSONB

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import get_redis_pool
from msai.models.portfolio_enums import PortfolioRunStatus
from msai.models.portfolio_run import PortfolioRun
from msai.services.compute_slots import (
    ComputeSlotUnavailableError,
    acquire_compute_slots,
    release_compute_slots,
    renew_compute_slots,
)
from msai.services.portfolio import (
    PortfolioOrchestrationError,
    PortfolioRunMemberFailureError,
    PortfolioRunTerminalStateError,
    PortfolioService,
)

if TYPE_CHECKING:
    from arq.connections import ArqRedis
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)


async def _check_cancel_flag(session: AsyncSession, run_id: UUID) -> bool:
    """Return True when the run row has been flipped to ``canceled``.

    Consulted by the Full-mode optimizer at the top of every trial (and
    by the Quick path's catalog warmup, future hook). Reads ``status``
    only — no commit needed.  Returns ``False`` on missing row so the
    optimizer's cancel loop doesn't conflate "row gone" with "cancelled"
    (the worker's other guards surface the missing-row case separately).
    """
    run = await session.get(PortfolioRun, run_id)
    return run is not None and run.status == PortfolioRunStatus.CANCELED.value


async def _portfolio_progress_callback(
    session: AsyncSession,
    run_id: UUID,
    pct: int,
    msg: str,
) -> None:
    """Persist progress (% complete + message) on the run row.

    Writes to ``metrics["progress"]`` / ``metrics["progress_message"]`` and
    refreshes ``heartbeat_at``. The UI polls these fields to render a
    progress bar without waiting for the run to land in a terminal state.

    Codex bot iter-9 P2 on PR #73: progress writes must NOT clobber
    completion metrics (best_config / IS / OOS). The previous
    "session.get + modify metrics dict + session.commit" approach
    captured the pre-completion metrics snapshot and overwrote terminal
    metrics on commit even with the iter-7 terminal-status early
    return — that guard only catches the case where the read sees the
    terminal status, not the race where the read precedes the
    completion commit AND the progress commit follows it.
    Two-layered fix:

    1. **Terminal-row early return** — defense in depth for late
       progress writes that fire AFTER completion already landed. The
       row's status is the cheapest signal of "stop, the run is done."
    2. **JSONB merge UPDATE** — the actual write is now a single
       PostgreSQL atomic UPDATE that uses ``metrics || {...}`` to
       MERGE the progress keys into whatever is currently in the row,
       instead of replacing the whole ``metrics`` JSONB blob. So
       progress writes and completion writes are commutative at the
       row level: progress before completion → completion replaces;
       completion before progress → progress merges its keys onto
       terminal metrics (terminal-row return catches that path before
       any merge).
    """
    run = await session.get(PortfolioRun, run_id)
    if run is None:
        return
    if PortfolioRunStatus(run.status).is_terminal:
        return

    # Atomic JSONB merge: ``metrics = COALESCE(metrics, '{}'::jsonb) || {progress, ...}::jsonb``.
    # PostgreSQL's ``||`` operator concatenates JSONB objects, with the right
    # side winning on key collision. So this preserves any keys that landed
    # between our session.get above and the UPDATE here — e.g., if Phase 3
    # commits ``best_config`` mid-flight, it will not be wiped by this write.
    await session.execute(
        update(PortfolioRun)
        .where(PortfolioRun.id == run_id)
        .values(
            metrics=func.coalesce(PortfolioRun.metrics, cast({}, JSONB)).op("||")(
                cast({"progress": int(pct), "progress_message": msg}, JSONB)
            ),
            heartbeat_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def _cancel_check_via_session(run_id: UUID) -> bool:
    """Open a session and consult :func:`_check_cancel_flag`.

    Convenience wrapper for the sync-bridge inside ``run_portfolio_job``:
    the optimizer runs in a worker thread and calls a sync callable; we
    schedule THIS coroutine on the main event loop so the DB read stays
    on the worker's existing async stack.
    """
    async with async_session_factory() as session:
        return await _check_cancel_flag(session, run_id)


async def _progress_via_session(run_id: UUID, pct: int, msg: str) -> None:
    """Open a session and persist progress via :func:`_portfolio_progress_callback`."""
    async with async_session_factory() as session:
        await _portfolio_progress_callback(session, run_id, pct, msg)


# Renew the compute-slot lease at roughly one-third of its TTL so it
# never expires under load.  Settings value is in seconds.
_RENEWAL_INTERVAL_SECONDS: int = max(5, settings.compute_slot_lease_seconds // 3)

# Read-after-write race guard — the API enqueues the arq job BEFORE
# committing the ``portfolio_runs`` row (matches the existing backtest
# pattern: enqueue first so a crashed commit releases the work, no
# orphan rows).  If the worker dequeues before the commit lands, the
# row lookup will briefly 404; retry a few times before giving up.
_START_LOOKUP_ATTEMPTS: int = 5
_START_LOOKUP_BACKOFF_SECONDS: float = 0.5


async def run_portfolio_job(
    ctx: dict[str, Any],
    run_id: str,
    portfolio_id: str,
) -> None:
    """Run a portfolio backtest end-to-end and persist results.

    Args:
        ctx: arq worker context (provides ``worker_instance_id``).
        run_id: UUID string of the :class:`PortfolioRun` row to execute.
        portfolio_id: UUID string of the owning :class:`Portfolio` (logged
            for observability; authoritative value lives on the run row).
    """
    run_uuid = UUID(run_id)
    worker_id = str(ctx.get("worker_instance_id") or f"{socket.gethostname()}:{os.getpid()}")
    service = PortfolioService()

    # arq's ``job_try`` is 1-indexed.  We need to know whether this is
    # the FINAL attempt before deciding to mark a row ``failed`` on a
    # transient error: marking failed on attempt 1 of 2 would prevent
    # attempt 2 from running (the terminal-state guard in
    # ``mark_run_running`` rejects ``failed → running``).  On the final
    # attempt, marking failed is the only way to surface the failure.
    job_try = int(ctx.get("job_try", 1))
    max_tries = int(ctx.get("max_tries", 2))
    is_final_attempt = job_try >= max_tries

    log.info(
        "portfolio_job_started",
        run_id=run_id,
        portfolio_id=portfolio_id,
        worker_id=worker_id,
        job_try=job_try,
        max_tries=max_tries,
    )

    # ---- Phase 1: flip to running (or bail) -------------------------------
    # Retry ``not found`` because the API enqueues BEFORE commit — the
    # row can lag the job by a few ms.  Surface terminal-state / genuine
    # missing-row distinctly from each other so the logs tell the
    # operator which case they hit.
    last_missing_error: PortfolioOrchestrationError | None = None
    for attempt in range(_START_LOOKUP_ATTEMPTS):
        try:
            async with async_session_factory() as session:
                await service.mark_run_running(session, run_uuid)
            last_missing_error = None
            break
        except PortfolioRunTerminalStateError:
            log.info(
                "portfolio_job_skipped_terminal_state",
                run_id=run_id,
                portfolio_id=portfolio_id,
            )
            return
        except PortfolioOrchestrationError as exc:
            last_missing_error = exc
            if attempt + 1 < _START_LOOKUP_ATTEMPTS:
                await asyncio.sleep(_START_LOOKUP_BACKOFF_SECONDS)
    if last_missing_error is not None:
        # Row still doesn't exist after the in-job retry window.  Two
        # plausible causes: (a) the API commit is unusually slow — give
        # arq a chance to re-deliver the job by re-raising; on retry the
        # commit should have landed.  (b) the job was enqueued for a row
        # that really doesn't exist (bug) — re-raise too; arq will retry
        # and on the FINAL attempt we mark-failed so the operator UI
        # surfaces it rather than the row being orphaned.
        log.error(
            "portfolio_run_not_found_at_start",
            run_id=run_id,
            in_job_attempts=_START_LOOKUP_ATTEMPTS,
            arq_attempt=job_try,
            error=str(last_missing_error),
        )
        # Re-raise regardless of attempt — if the row doesn't exist,
        # ``_mark_failed_safe`` can't actually mark it failed, so
        # returning ``None`` would silently ack the arq job and the row
        # (once it eventually commits) would stay ``pending`` forever
        # with nothing to execute it.  Raising keeps the failure visible
        # in arq's DLQ on the final attempt and lets earlier attempts
        # retry.  The residual stuck-pending case — where the row lands
        # after all arq retries are exhausted — is a known gap that the
        # future job_watchdog scan for portfolio_runs will resolve.
        raise last_missing_error

    # ---- Phase 2: acquire Redis + compute slots ---------------------------
    # Redis outage is a transient infra failure.  On non-final attempts,
    # leave the row ``running`` and re-raise so arq retries — marking
    # failed first would lock the row out of attempt 2 via the
    # terminal-state guard.  Only mark failed if this is the last try.
    redis: ArqRedis
    try:
        redis = await get_redis_pool()
    except Exception as exc:  # noqa: BLE001 — infra-level, conditionally mark.
        log.exception("portfolio_job_redis_unavailable", run_id=run_id)
        if is_final_attempt:
            await _mark_failed_safe(service, run_uuid, f"Redis unavailable: {exc}")
        raise

    lease_id: str | None = None
    renewal_task: asyncio.Task[None] | None = None
    try:
        async with async_session_factory() as session:
            run = await service.get_run(session, run_uuid)
            allocations = await service.get_allocations(session, run.portfolio_id)
            # When ``max_parallelism`` is omitted by the caller (the
            # default UI path sends only dates), default to the full
            # available cluster budget rather than forcing serial
            # execution — a 4-candidate portfolio would otherwise run
            # 4× longer than necessary and risk the portfolio job
            # timeout for no reason.
            requested = (
                max(1, int(run.max_parallelism))
                if run.max_parallelism is not None
                else settings.compute_slot_limit
            )
            # Only reserve what we can actually use — a small portfolio
            # with a high ``max_parallelism`` must not hog the cluster
            # semaphore while running a handful of backtests.
            slot_count = max(
                1,
                min(requested, len(allocations), settings.compute_slot_limit),
            )

        try:
            lease_id = await acquire_compute_slots(
                redis,
                job_kind="portfolio",
                job_id=run_id,
                slot_count=slot_count,
            )
        except ComputeSlotUnavailableError as exc:
            log.warning(
                "portfolio_slots_unavailable",
                run_id=run_id,
                slot_count=slot_count,
                error=str(exc),
            )
            await _mark_failed_safe(service, run_uuid, f"Compute slots unavailable: {exc}")
            return  # Not an infra error — no reason to retry via arq.

        # Start lease-renewal loop before kicking off the long-running
        # orchestration so the lease never expires mid-backtest.  The
        # loop also refreshes ``portfolio_runs.heartbeat_at`` so a
        # future stale-job scanner can distinguish actively-executing
        # runs from abandoned ``running`` rows.
        renewal_task = asyncio.create_task(
            _renew_lease_forever(redis, lease_id, run_id=run_id, service=service)
        )

        # ---- Phase 3: orchestrate ----------------------------------------
        # ``max_workers`` is a HARD cap equal to the lease we hold — the
        # service re-reads allocations in its own session and could
        # otherwise (harmlessly but wastefully) launch more threads than
        # slots we reserved.  Passing it explicitly pins the semaphore.
        #
        # Cancellation + progress: the optimizer's ``cancel_check`` is a
        # sync callable (called between trials) and ``progress_callback``
        # is sync too.  Both need to talk to the DB.  We schedule the
        # async helpers onto the worker's event loop via
        # ``run_coroutine_threadsafe`` so the sync optimizer thread blocks
        # only for the few ms each call takes.
        worker_loop = asyncio.get_running_loop()

        def _sync_cancel_check() -> bool:
            future = asyncio.run_coroutine_threadsafe(
                _cancel_check_via_session(run_uuid),
                worker_loop,
            )
            try:
                # Bounded wait — DB read should be <100ms; a longer hang
                # means Postgres is sick and we should let the optimizer
                # keep going rather than freeze the trial loop.
                return bool(future.result(timeout=5.0))
            except Exception:  # noqa: BLE001 — best-effort cancel poll
                log.warning("portfolio_cancel_check_failed", run_id=run_id)
                return False

        def _sync_progress(pct: int, msg: str) -> None:
            asyncio.run_coroutine_threadsafe(
                _progress_via_session(run_uuid, pct, msg),
                worker_loop,
            )
            # Fire-and-forget — we don't wait for the write.  If the
            # write fails, the run still finishes; the only consequence
            # is that the UI's progress bar will be stale.

        await service.run_portfolio_backtest(
            run_uuid,
            max_workers=slot_count,
            cancel_check=_sync_cancel_check,
            progress_callback=_sync_progress,
        )

        log.info(
            "portfolio_job_completed",
            run_id=run_id,
            portfolio_id=portfolio_id,
            worker_id=worker_id,
        )

    except PortfolioRunMemberFailureError as exc:
        # Quick-mode per-member failure with attribution — persist the
        # structured ``per_strategy_errors`` payload onto the run row's
        # ``metrics`` BEFORE marking failed so the failure block survives
        # the terminal-state guard (mark_run_failed refuses to overwrite
        # any subsequent state). PRD US-002a's contract is "operator sees
        # which member raised" — this is the on-row evidence.
        log.warning(
            "portfolio_job_member_failure",
            run_id=run_id,
            portfolio_id=portfolio_id,
            num_failed=len(exc.per_strategy_errors),
            error_type=type(exc).__name__,
        )
        await _persist_per_strategy_errors(run_uuid, exc.per_strategy_errors)
        await _mark_failed_safe(service, run_uuid, str(exc))
    except (PortfolioOrchestrationError, FileNotFoundError, TimeoutError, ValueError) as exc:
        # Deterministic failures — retry won't help.
        #   * ``PortfolioOrchestrationError``: data-shape problem
        #     (missing candidate, no instruments, etc.).
        #   * ``FileNotFoundError``: a candidate's source Parquet data
        #     is absent (raised by ``ensure_catalog_data``).
        #   * ``TimeoutError``: a single backtest exceeded
        #     ``backtest_timeout_seconds`` — rerunning will time out
        #     the same way.  Operator must tune the timeout or fix the
        #     strategy before re-running.
        #   * ``ValueError``: invalid run inputs (e.g. Full-mode date
        #     range too short for any walk-forward window to fit, raised
        #     by ``build_walk_forward_windows``).  These raise BEFORE the
        #     first progress callback / heartbeat — without this branch
        #     the generic ``except Exception`` below would only mark the
        #     row failed on the FINAL arq attempt, leaving the run row
        #     stuck in ``running`` for the entire retry window.  Same
        #     date range will fail identically on retry, so we mark
        #     failed eagerly and skip the arq retry.
        # Mark failed + do NOT re-raise so arq does not waste a retry.
        log.warning(
            "portfolio_job_data_error",
            run_id=run_id,
            portfolio_id=portfolio_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        await _mark_failed_safe(service, run_uuid, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — infra-level, conditional mark + re-raise.
        log.exception(
            "portfolio_job_infrastructure_failure",
            run_id=run_id,
            portfolio_id=portfolio_id,
            error_type=type(exc).__name__,
        )
        # Only mark failed on the FINAL arq attempt — otherwise the
        # terminal-state guard would block attempt 2 from running.
        if is_final_attempt:
            await _mark_failed_safe(service, run_uuid, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if renewal_task is not None:
            renewal_task.cancel()
            try:
                await renewal_task
            except asyncio.CancelledError:
                # Expected — we issued the cancel ourselves.
                pass
            except Exception:  # noqa: BLE001 — renewal failures are logged inside.
                log.exception("portfolio_slots_renew_task_failed", run_id=run_id)
        if lease_id is not None:
            try:
                await release_compute_slots(redis, lease_id)
            except Exception:  # noqa: BLE001 — release best-effort.
                log.exception("portfolio_slots_release_failed", lease_id=lease_id)


async def _renew_lease_forever(
    redis: ArqRedis,
    lease_id: str,
    *,
    run_id: str,
    service: PortfolioService,
) -> None:
    """Background task that renews the compute-slot lease AND DB heartbeat.

    Two independent refresh jobs run on the same cadence: the Redis
    compute-slot lease (so other jobs can't reclaim the slots while we
    work) and the ``portfolio_runs.heartbeat_at`` column (so a future
    stale-job scanner can distinguish a live run from an abandoned
    ``running`` row).

    Both are best-effort: renewal failures warn but don't abort the
    task.  The lease TTL will expire naturally if this loop dies; the
    heartbeat will go stale — both are acceptable degradation modes.
    """
    run_uuid = UUID(run_id)
    try:
        while True:
            await asyncio.sleep(_RENEWAL_INTERVAL_SECONDS)
            try:
                await renew_compute_slots(redis, lease_id)
            except Exception:  # noqa: BLE001 — renewal is best-effort.
                log.warning(
                    "portfolio_slots_renew_failed",
                    run_id=run_id,
                    lease_id=lease_id,
                )
            try:
                async with async_session_factory() as session:
                    await service.heartbeat_run(session, run_uuid)
            except Exception:  # noqa: BLE001 — heartbeat is best-effort.
                log.warning("portfolio_heartbeat_refresh_failed", run_id=run_id)
    except asyncio.CancelledError:
        raise


async def _persist_per_strategy_errors(
    run_id: UUID,
    per_strategy_errors: list[dict[str, str]],
) -> None:
    """Persist :class:`PortfolioRunMemberFailureError`'s payload onto the run row.

    Must run BEFORE :func:`_mark_failed_safe` because the lifecycle's
    terminal-state guard refuses further status writes once ``failed`` is
    set; persisting the diagnostic AFTER ``mark_run_failed`` would silently
    no-op. Best-effort: a DB outage here drops the per-strategy detail but
    the run still gets the summary error message via ``mark_run_failed``.
    """
    try:
        async with async_session_factory() as session:
            run = await session.get(PortfolioRun, run_id)
            if run is None:
                log.warning(
                    "portfolio_per_strategy_errors_skip_missing_row",
                    run_id=str(run_id),
                )
                return
            metrics = dict(run.metrics or {})
            metrics["per_strategy_errors"] = per_strategy_errors
            run.metrics = metrics
            await session.commit()
    except Exception:  # noqa: BLE001 — best-effort diagnostic persistence
        log.exception("portfolio_per_strategy_errors_persist_failed", run_id=str(run_id))


async def _mark_failed_safe(
    service: PortfolioService,
    run_id: UUID,
    error_message: str,
) -> None:
    """Mark a run ``failed`` — never raises (best-effort logging only).

    A DB outage during failure-marking leaves the run in whatever state
    it was last in (typically ``running``); the warning log is the only
    signal.  This is documented as a known gap — full recovery depends
    on the heartbeat/stale-job scanner (Phase 2 scheduler port).
    """
    try:
        async with async_session_factory() as session:
            await service.mark_run_failed(session, run_id, error_message=error_message)
    except PortfolioRunTerminalStateError:
        # Already completed — leave the happy result in place.
        log.warning(
            "portfolio_run_failed_update_skipped_terminal",
            run_id=str(run_id),
        )
    except Exception:  # noqa: BLE001 — best-effort in the error path.
        log.exception("portfolio_run_failed_update_failed", run_id=str(run_id))
