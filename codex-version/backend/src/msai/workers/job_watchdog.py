from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import get_redis_pool, queued_job_state, remove_queued_job
from msai.models import Backtest
from msai.services.alerting import alerting_service
from msai.services.portfolio_service import PortfolioService
from msai.services.research_jobs import ResearchJobService

logger = get_logger("workers.job_watchdog")


async def run_watchdog_once() -> None:
    pool = await get_redis_pool()
    await _scan_research_jobs(pool)
    await _scan_backtest_jobs(pool)
    await _scan_portfolio_runs(pool)


async def _scan_research_jobs(pool) -> None:
    job_service = ResearchJobService()
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=settings.research_job_stale_seconds)
    pending_cutoff = now - timedelta(seconds=settings.research_job_pending_grace_seconds)

    for job in job_service.list_jobs(limit=1000):
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        status = str(job.get("status") or "pending")
        queue_name = str(job.get("queue_name") or settings.research_queue_name)
        queue_job_id = job.get("queue_job_id")
        cancel_requested = bool(job.get("cancel_requested"))

        if not isinstance(queue_job_id, str) or not queue_job_id:
            continue

        queue_state = await queued_job_state(pool, queue_name=queue_name, queue_job_id=queue_job_id)

        if cancel_requested and status in {"pending", "cancelling"}:
            await remove_queued_job(pool, queue_name=queue_name, queue_job_id=queue_job_id)
            job_service.mark_cancelled(job_id, message="Cancelled by watchdog")
            logger.info("research_job_cancelled_by_watchdog", job_id=job_id)
            continue

        if status == "pending":
            created_at = _parse_iso(job.get("created_at"))
            if queue_state is None and created_at is not None and created_at < pending_cutoff:
                job_service.mark_failed(
                    job_id,
                    error_message="Queued research job disappeared from Redis before execution",
                )
                alerting_service.send_alert(
                    "error",
                    "Research job orphaned",
                    f"job_id={job_id} queue_job_id={queue_job_id} disappeared before execution",
                )
            continue

        if status not in {"running", "cancelling"}:
            continue

        heartbeat_at = _parse_iso(job.get("heartbeat_at") or job.get("started_at"))
        if heartbeat_at is None or heartbeat_at >= stale_cutoff:
            continue

        await remove_queued_job(pool, queue_name=queue_name, queue_job_id=queue_job_id)
        if cancel_requested or status == "cancelling":
            job_service.mark_cancelled(job_id, message="Cancelled after stale heartbeat")
            alerting_service.send_alert(
                "warning",
                "Research job cancelled after stall",
                f"job_id={job_id} worker_id={job.get('worker_id')} stale heartbeat at {job.get('heartbeat_at')}",
            )
        else:
            job_service.mark_failed(
                job_id,
                error_message="Research job heartbeat went stale; watchdog cleaned Redis state",
            )
            alerting_service.send_alert(
                "error",
                "Research job stalled",
                f"job_id={job_id} worker_id={job.get('worker_id')} stale heartbeat at {job.get('heartbeat_at')}",
            )
        logger.warning(
            "research_job_stale_cleaned",
            job_id=job_id,
            queue_job_id=queue_job_id,
            status=status,
        )


async def _scan_backtest_jobs(pool) -> None:
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=settings.backtest_job_stale_seconds)
    pending_cutoff = now - timedelta(seconds=settings.backtest_job_pending_grace_seconds)

    async with async_session_factory() as session:
        rows = await session.execute(select(Backtest).where(Backtest.status.in_(("pending", "running"))).limit(1000))
        backtests = list(rows.scalars())

    for backtest in backtests:
        backtest_id = str(backtest.id)
        status = str(backtest.status or "pending")
        queue_job_id = str(backtest.queue_job_id or backtest_id)
        queue_name = str(backtest.queue_name or settings.backtest_queue_name)
        queue_state = await queued_job_state(pool, queue_name=queue_name, queue_job_id=queue_job_id)

        if status == "pending":
            created_at = backtest.created_at
            if created_at is not None and queue_state is None and created_at < pending_cutoff:
                await _mark_backtest_failed(
                    backtest_id,
                    "Queued backtest job disappeared from Redis before execution",
                )
                alerting_service.send_alert(
                    "error",
                    "Backtest job orphaned",
                    f"backtest_id={backtest_id} queue_job_id={queue_job_id} disappeared before execution",
                )
            continue

        heartbeat_at = backtest.heartbeat_at or backtest.started_at
        if heartbeat_at is None or heartbeat_at >= stale_cutoff:
            continue

        await remove_queued_job(pool, queue_name=queue_name, queue_job_id=queue_job_id)
        await _mark_backtest_failed(
            backtest_id,
            "Backtest job heartbeat went stale; watchdog cleaned Redis state",
        )
        alerting_service.send_alert(
            "error",
            "Backtest job stalled",
            f"backtest_id={backtest_id} worker_id={backtest.worker_id} stale heartbeat at {heartbeat_at.isoformat()}",
        )
        logger.warning(
            "backtest_job_stale_cleaned",
            backtest_id=backtest_id,
            queue_job_id=queue_job_id,
            status=status,
        )


async def _scan_portfolio_runs(pool) -> None:
    service = PortfolioService()
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=settings.portfolio_job_stale_seconds)
    pending_cutoff = now - timedelta(seconds=settings.portfolio_job_pending_grace_seconds)

    for run in service.list_runs(limit=1000):
        run_id = str(run.get("id") or "")
        if not run_id:
            continue
        status = str(run.get("status") or "pending")
        queue_job_id = str(run.get("queue_job_id") or run_id)
        queue_name = str(run.get("queue_name") or settings.portfolio_queue_name)
        queue_state = await queued_job_state(pool, queue_name=queue_name, queue_job_id=queue_job_id)

        if status == "pending":
            created_at = _parse_iso(run.get("created_at"))
            if created_at is not None and queue_state is None and created_at < pending_cutoff:
                service.mark_run_failed(
                    run_id,
                    error_message="Queued portfolio run disappeared from Redis before execution",
                )
                alerting_service.send_alert(
                    "error",
                    "Portfolio run orphaned",
                    f"run_id={run_id} queue_job_id={queue_job_id} disappeared before execution",
                )
            continue

        if status != "running":
            continue

        heartbeat_at = _parse_iso(run.get("heartbeat_at") or run.get("updated_at"))
        if heartbeat_at is None or heartbeat_at >= stale_cutoff:
            continue

        await remove_queued_job(pool, queue_name=queue_name, queue_job_id=queue_job_id)
        service.mark_run_failed(
            run_id,
            error_message="Portfolio run heartbeat went stale; watchdog cleaned Redis state",
        )
        alerting_service.send_alert(
            "error",
            "Portfolio run stalled",
            f"run_id={run_id} worker_id={run.get('worker_id')} stale heartbeat at {run.get('heartbeat_at')}",
        )
        logger.warning(
            "portfolio_run_stale_cleaned",
            run_id=run_id,
            queue_job_id=queue_job_id,
            status=status,
        )


async def _mark_backtest_failed(backtest_id: str, error_message: str) -> None:
    async with async_session_factory() as session:
        row = await session.get(Backtest, backtest_id)
        if row is None:
            return
        row.status = "failed"
        row.error_message = error_message
        row.completed_at = datetime.now(UTC)
        row.heartbeat_at = datetime.now(UTC)
        await session.commit()


async def run_watchdog_forever() -> None:
    logger.info(
        "job_watchdog_started",
        poll_seconds=settings.job_watchdog_poll_seconds,
        stale_seconds=settings.research_job_stale_seconds,
    )
    while True:
        try:
            await run_watchdog_once()
        except Exception as exc:  # pragma: no cover - defensive loop logging
            logger.exception("job_watchdog_iteration_failed", error=str(exc))
            alerting_service.send_alert("error", "Job watchdog failed", str(exc))
        await asyncio.sleep(settings.job_watchdog_poll_seconds)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def main() -> None:
    asyncio.run(run_watchdog_forever())


if __name__ == "__main__":
    main()
