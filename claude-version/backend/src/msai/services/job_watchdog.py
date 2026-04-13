"""Job watchdog — detects stale and orphaned background jobs.

Periodically scans the ``backtests`` and ``research_jobs`` tables for rows
that are still ``pending`` or ``running`` but have stopped making progress
(stale heartbeat or stuck in pending).  Affected rows are marked ``failed``
with a descriptive error message so the UI can surface them to the user.

Designed to run as an arq cron job (see :mod:`msai.workers.settings`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models.backtest import Backtest
from msai.models.research_job import ResearchJob

log = get_logger(__name__)


async def run_watchdog_once() -> dict[str, int]:
    """Scan for stale/orphaned jobs and mark them failed.

    Returns:
        A dict with counts of cleaned jobs per table.
    """
    async with async_session_factory() as session:
        backtests_cleaned = await _scan_backtests(session)
        research_cleaned = await _scan_research_jobs(session)
        await session.commit()
    return {
        "backtests_cleaned": backtests_cleaned,
        "research_cleaned": research_cleaned,
    }


async def _scan_backtests(session: AsyncSession) -> int:
    """Find and mark stale/stuck backtests as failed.

    Returns:
        Number of backtests cleaned.
    """
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=settings.job_stale_seconds)
    pending_cutoff = now - timedelta(seconds=settings.job_pending_grace_seconds)

    stmt = select(Backtest).where(Backtest.status.in_(["pending", "running"]))
    result = await session.execute(stmt)
    rows = result.scalars().all()

    cleaned = 0
    for backtest in rows:
        reason = _check_job_health(
            status=backtest.status,
            heartbeat_at=backtest.heartbeat_at,
            created_at=backtest.created_at,
            stale_cutoff=stale_cutoff,
            pending_cutoff=pending_cutoff,
            now=now,
        )
        if reason is None:
            continue

        backtest.status = "failed"
        backtest.error_message = reason
        backtest.completed_at = now
        cleaned += 1
        log.warning(
            "watchdog_backtest_cleaned",
            backtest_id=str(backtest.id),
            prior_status=backtest.status,
            reason=reason,
        )

    return cleaned


async def _scan_research_jobs(session: AsyncSession) -> int:
    """Find and mark stale/stuck research jobs as failed.

    Returns:
        Number of research jobs cleaned.
    """
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=settings.job_stale_seconds)
    pending_cutoff = now - timedelta(seconds=settings.job_pending_grace_seconds)

    stmt = select(ResearchJob).where(ResearchJob.status.in_(["pending", "running"]))
    result = await session.execute(stmt)
    rows = result.scalars().all()

    cleaned = 0
    for job in rows:
        reason = _check_job_health(
            status=job.status,
            heartbeat_at=job.heartbeat_at,
            created_at=job.created_at,
            stale_cutoff=stale_cutoff,
            pending_cutoff=pending_cutoff,
            now=now,
        )
        if reason is None:
            continue

        job.status = "failed"
        job.error_message = reason
        job.completed_at = now
        cleaned += 1
        log.warning(
            "watchdog_research_job_cleaned",
            research_job_id=str(job.id),
            prior_status=job.status,
            reason=reason,
        )

    return cleaned


def _check_job_health(
    *,
    status: str,
    heartbeat_at: datetime | None,
    created_at: datetime,
    stale_cutoff: datetime,
    pending_cutoff: datetime,
    now: datetime,
) -> str | None:
    """Return a failure reason if the job is stale/stuck, or ``None`` if healthy.

    Args:
        status: Current job status (``"pending"`` or ``"running"``).
        heartbeat_at: Last heartbeat timestamp (may be ``None``).
        created_at: When the job was created.
        stale_cutoff: Threshold — running jobs with heartbeat before this are stale.
        pending_cutoff: Threshold — pending jobs created before this are stuck.
        now: Current UTC time.

    Returns:
        A human-readable failure reason, or ``None`` if the job is healthy.
    """
    if status == "running" and heartbeat_at is not None and heartbeat_at < stale_cutoff:
        elapsed = int((now - heartbeat_at).total_seconds())
        return f"Watchdog: no heartbeat for {elapsed} seconds"

    if status == "pending" and created_at < pending_cutoff:
        elapsed = int((now - created_at).total_seconds())
        return f"Watchdog: stuck in pending for {elapsed} seconds"

    return None
