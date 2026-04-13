"""arq worker function for portfolio-level backtest runs.

Lifecycle for a single job:

1. Fetch the :class:`PortfolioRun` row by ID and flip its status to ``running``.
2. (Phase 2) Orchestrate combined backtests across all allocated strategies.
3. Mark the run ``completed`` (or ``failed`` on error).

The actual multi-strategy orchestration is Phase 2.  This job currently
acts as a placeholder that validates the row exists and exercises the
full pending -> running -> completed state machine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models.portfolio_run import PortfolioRun

log = get_logger(__name__)


async def run_portfolio_job(
    ctx: dict[str, Any],
    run_id: str,
    portfolio_id: str,
) -> None:
    """Run a portfolio backtest end-to-end and persist results.

    This is the function the arq worker dispatches when it picks up a
    ``run_portfolio`` job off the Redis queue.

    Args:
        ctx: arq worker context (unused here but part of the arq contract).
        run_id: UUID string of the :class:`PortfolioRun` row to execute.
        portfolio_id: UUID string of the owning :class:`Portfolio`.
    """
    _ = ctx
    log.info(
        "portfolio_job_started",
        run_id=run_id,
        portfolio_id=portfolio_id,
    )

    # --- 1. Mark running ---------------------------------------------------
    try:
        async with async_session_factory() as session:
            run = await session.get(PortfolioRun, run_id)
            if run is None:
                log.error("portfolio_run_not_found", run_id=run_id)
                return
            run.status = "running"
            await session.commit()
    except Exception:
        log.exception("portfolio_run_start_failed", run_id=run_id)
        return

    # --- 2. Placeholder for multi-strategy orchestration (Phase 2) ---------
    try:
        log.info(
            "portfolio_job_placeholder",
            run_id=run_id,
            portfolio_id=portfolio_id,
            message="Multi-strategy orchestration is Phase 2. Marking completed.",
        )

        # --- 3. Mark completed -------------------------------------------------
        async with async_session_factory() as session:
            run = await session.get(PortfolioRun, run_id)
            if run is None:
                return
            run.status = "completed"
            run.completed_at = datetime.now(UTC)
            run.metrics = {
                "note": "Placeholder — real orchestration is Phase 2",
            }
            await session.commit()

        log.info("portfolio_job_completed", run_id=run_id)

    except Exception as exc:
        log.exception("portfolio_job_failed", run_id=run_id, error=str(exc))
        await _mark_run_failed(run_id, str(exc))


async def _mark_run_failed(run_id: str, error_message: str) -> None:
    """Update a portfolio run row to ``failed`` with a user-visible error message."""
    try:
        async with async_session_factory() as session:
            run = await session.get(PortfolioRun, run_id)
            if run is None:
                return
            run.status = "failed"
            run.completed_at = datetime.now(UTC)
            run.metrics = {"error": error_message}
            await session.commit()
    except Exception:
        log.exception("portfolio_run_status_update_failed", run_id=run_id)
