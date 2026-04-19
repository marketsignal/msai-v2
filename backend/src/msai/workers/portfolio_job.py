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
from typing import TYPE_CHECKING, Any
from uuid import UUID

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import get_redis_pool
from msai.services.compute_slots import (
    ComputeSlotUnavailableError,
    acquire_compute_slots,
    release_compute_slots,
    renew_compute_slots,
)
from msai.services.portfolio_service import (
    PortfolioOrchestrationError,
    PortfolioRunTerminalStateError,
    PortfolioService,
)

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = get_logger(__name__)

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
        await service.run_portfolio_backtest(run_uuid, max_workers=slot_count)

        log.info(
            "portfolio_job_completed",
            run_id=run_id,
            portfolio_id=portfolio_id,
            worker_id=worker_id,
        )

    except (PortfolioOrchestrationError, FileNotFoundError, TimeoutError) as exc:
        # Deterministic failures — retry won't help.
        #   * ``PortfolioOrchestrationError``: data-shape problem
        #     (missing candidate, no instruments, etc.).
        #   * ``FileNotFoundError``: a candidate's source Parquet data
        #     is absent (raised by ``ensure_catalog_data``).
        #   * ``TimeoutError``: a single backtest exceeded
        #     ``backtest_timeout_seconds`` — rerunning will time out
        #     the same way.  Operator must tune the timeout or fix the
        #     strategy before re-running.
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
