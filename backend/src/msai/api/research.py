"""Research API router — launch, monitor, and promote research jobs.

Endpoints for submitting parameter sweeps and walk-forward optimisations,
listing / polling job status, and promoting the best result to a
:class:`GraduationCandidate` for the graduation pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user, resolve_user_id
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.core.queue import enqueue_research, get_redis_pool
from msai.models.research_job import ResearchJob
from msai.models.research_trial import ResearchTrial
from msai.models.strategy import Strategy
from msai.schemas.research import (
    ResearchJobDetailResponse,
    ResearchJobListResponse,
    ResearchJobResponse,
    ResearchPromotionRequest,
    ResearchPromotionResponse,
    ResearchSweepRequest,
    ResearchTrialResponse,
    ResearchWalkForwardRequest,
)
from msai.services.graduation import GraduationService

log = get_logger(__name__)

_graduation_service = GraduationService()

router = APIRouter(prefix="/api/v1/research", tags=["research"])


# ---------------------------------------------------------------------------
# POST /sweeps — submit parameter sweep job
# ---------------------------------------------------------------------------


@router.post("/sweeps", status_code=status.HTTP_201_CREATED, response_model=ResearchJobResponse)
async def submit_parameter_sweep(
    body: ResearchSweepRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ResearchJobResponse:
    """Create a new parameter sweep research job and enqueue it."""
    strategy = await _resolve_strategy(db, body.strategy_id)
    strategy_path = _resolve_strategy_path(strategy)
    payload = _build_sweep_payload(body, strategy, strategy_path)
    user_id = await resolve_user_id(db, claims)

    job = ResearchJob(
        strategy_id=body.strategy_id,
        job_type="parameter_sweep",
        config=payload,
        status="pending",
        progress=0,
        created_by=user_id,
    )
    db.add(job)
    await db.flush()

    queue_job_id = await _enqueue_job(db, job, "parameter_sweep", payload)
    job.queue_job_id = queue_job_id
    job.queue_name = settings.research_queue_name
    await db.commit()
    await db.refresh(job)

    log.info(
        "research_sweep_enqueued",
        job_id=str(job.id),
        strategy_id=str(body.strategy_id),
    )
    return ResearchJobResponse.model_validate(job)


# ---------------------------------------------------------------------------
# POST /walk-forward — submit walk-forward optimisation job
# ---------------------------------------------------------------------------


@router.post(
    "/walk-forward",
    status_code=status.HTTP_201_CREATED,
    response_model=ResearchJobResponse,
)
async def submit_walk_forward(
    body: ResearchWalkForwardRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ResearchJobResponse:
    """Create a new walk-forward optimisation job and enqueue it."""
    strategy = await _resolve_strategy(db, body.strategy_id)
    strategy_path = _resolve_strategy_path(strategy)
    payload = _build_sweep_payload(body, strategy, strategy_path)
    payload["train_days"] = body.train_days
    payload["test_days"] = body.test_days
    payload["step_days"] = body.step_days
    payload["mode"] = body.mode
    user_id = await resolve_user_id(db, claims)

    job = ResearchJob(
        strategy_id=body.strategy_id,
        job_type="walk_forward",
        config=payload,
        status="pending",
        progress=0,
        created_by=user_id,
    )
    db.add(job)
    await db.flush()

    queue_job_id = await _enqueue_job(db, job, "walk_forward", payload)
    job.queue_job_id = queue_job_id
    job.queue_name = settings.research_queue_name
    await db.commit()
    await db.refresh(job)

    log.info(
        "research_walk_forward_enqueued",
        job_id=str(job.id),
        strategy_id=str(body.strategy_id),
    )
    return ResearchJobResponse.model_validate(job)


# ---------------------------------------------------------------------------
# GET /jobs — list research jobs (paginated)
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=ResearchJobListResponse)
async def list_research_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ResearchJobListResponse:
    """List research jobs with pagination, most recent first."""
    count_result = await db.execute(select(func.count()).select_from(ResearchJob))
    total: int = count_result.scalar_one()

    offset = (page - 1) * page_size
    result = await db.execute(
        select(ResearchJob).order_by(ResearchJob.created_at.desc()).offset(offset).limit(page_size)
    )
    jobs = result.scalars().all()

    return ResearchJobListResponse(
        items=[ResearchJobResponse.model_validate(j) for j in jobs],
        total=total,
    )


# ---------------------------------------------------------------------------
# GET /jobs/{job_id} — job detail with trials
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}", response_model=ResearchJobDetailResponse)
async def get_research_job(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ResearchJobDetailResponse:
    """Return a research job with its trials."""
    job = await db.get(ResearchJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Research job {job_id} not found",
        )

    trials_result = await db.execute(
        select(ResearchTrial)
        .where(ResearchTrial.research_job_id == job_id)
        .order_by(ResearchTrial.trial_number)
    )
    trials = trials_result.scalars().all()

    return ResearchJobDetailResponse(
        **ResearchJobResponse.model_validate(job).model_dump(),
        config=job.config,
        results=job.results,
        trials=[ResearchTrialResponse.model_validate(t) for t in trials],
    )


# ---------------------------------------------------------------------------
# POST /jobs/{job_id}/cancel — request cancellation
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/cancel", response_model=ResearchJobResponse)
async def cancel_research_job(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ResearchJobResponse:
    """Request cancellation of a research job.

    Terminal jobs (completed, failed, cancelled) are returned unchanged.
    Pending jobs are marked cancelled immediately.  Running jobs get a
    ``cancel_requested`` flag that the worker checks on next heartbeat.
    """
    job = await db.get(ResearchJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Research job {job_id} not found",
        )

    if job.status in {"completed", "failed", "cancelled"}:
        return ResearchJobResponse.model_validate(job)

    if job.status == "pending":
        job.status = "cancelled"
        job.progress_message = "Cancelled before starting"
    else:
        # Running — set status to "cancelled"; the worker's heartbeat loop
        # polls for this status and sets the cancel_requested event.
        job.status = "cancelled"

    await db.commit()
    await db.refresh(job)
    return ResearchJobResponse.model_validate(job)


# ---------------------------------------------------------------------------
# POST /promotions — promote best result to graduation candidate
# ---------------------------------------------------------------------------


@router.post(
    "/promotions",
    status_code=status.HTTP_201_CREATED,
    response_model=ResearchPromotionResponse,
)
async def promote_research_result(
    body: ResearchPromotionRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ResearchPromotionResponse:
    """Promote the best result from a completed research job to a graduation candidate."""
    job = await db.get(ResearchJob, body.research_job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Research job {body.research_job_id} not found",
        )

    if job.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Research job is {job.status}, not completed — cannot promote",
        )

    if job.best_config is None or job.best_metrics is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Research job has no best result to promote",
        )

    # Select specific trial if requested
    config = dict(job.best_config)
    metrics = dict(job.best_metrics)
    if body.trial_index is not None:
        trial_result = await db.execute(
            select(ResearchTrial).where(
                ResearchTrial.research_job_id == body.research_job_id,
                ResearchTrial.trial_number == body.trial_index,
            )
        )
        trial = trial_result.scalar_one_or_none()
        if trial is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Trial {body.trial_index} not found in job {body.research_job_id}",
            )
        config = dict(trial.config)
        metrics = dict(trial.metrics) if trial.metrics else {}

    # Bug #3 (live-deploy-safety-trio): stamp instruments into the
    # candidate's config so the snapshot-binding verifier at
    # /start-portfolio has the authoritative graduated instrument list.
    # Before this fix, research best/trial configs were built via
    # `{**base_config, **params}` at research_engine.py:636 — instruments
    # were a separate top-level request field and never made it into
    # `candidate.config`. Now stamped explicitly at the promotion
    # boundary; pre-Bug-#3 candidates are repaired via
    # `scripts/backfill_candidate_instruments.py`.
    job_instruments = job.config.get("instruments") if isinstance(job.config, dict) else None
    if isinstance(job_instruments, list) and job_instruments:
        config["instruments"] = list(job_instruments)

    user_id = await resolve_user_id(db, claims)
    candidate = await _graduation_service.create_candidate(
        db,
        strategy_id=job.strategy_id,
        config=config,
        metrics=metrics,
        research_job_id=job.id,
        notes=body.notes,
        user_id=user_id,
    )
    await db.commit()
    await db.refresh(candidate)

    log.info(
        "research_result_promoted",
        candidate_id=str(candidate.id),
        research_job_id=str(job.id),
        strategy_id=str(job.strategy_id),
    )
    return ResearchPromotionResponse(
        candidate_id=candidate.id,
        stage=candidate.stage,
        message=f"Promoted to graduation candidate (stage: {candidate.stage})",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _resolve_strategy(db: AsyncSession, strategy_id: UUID) -> Strategy:
    """Load a strategy by ID or raise 404."""
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy: Strategy | None = result.scalar_one_or_none()
    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {strategy_id} not found",
        )
    return strategy


def _resolve_strategy_path(strategy: Strategy) -> str:
    """Validate the strategy file exists on disk and return its path.

    Also enforces that the resolved path is under ``strategies_root`` to
    prevent path-traversal attacks (e.g. ``../../etc/passwd``).
    """
    if not strategy.file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Strategy {strategy.id} has no file_path configured",
        )
    strategy_file = Path(strategy.file_path)
    if not strategy_file.exists():
        # Try resolving relative to strategies_root
        strategy_file = settings.strategies_root / strategy.file_path
        if not strategy_file.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Strategy file not found: {strategy.file_path}",
            )

    resolved = strategy_file.resolve()
    strategies_root = settings.strategies_root.resolve()
    if not str(resolved).startswith(str(strategies_root)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Strategy path outside allowed directory",
        )
    return str(resolved)


def _build_sweep_payload(
    body: ResearchSweepRequest,
    strategy: Strategy,
    strategy_path: str,
) -> dict[str, Any]:
    """Build the payload dict that gets stored in the DB and forwarded to the worker."""
    return {
        "strategy_id": str(strategy.id),
        "strategy_name": strategy.name,
        "strategy_path": strategy_path,
        "instruments": body.instruments,
        "asset_class": body.asset_class,
        "start_date": body.start_date.isoformat(),
        "end_date": body.end_date.isoformat(),
        "base_config": body.base_config,
        "parameter_grid": {k: list(v) for k, v in body.parameter_grid.items()},
        "objective": body.objective,
        "max_parallelism": body.max_parallelism,
        "search_strategy": body.search_strategy,
        "min_trades": body.min_trades,
        "require_positive_return": body.require_positive_return,
        "holdout_fraction": body.holdout_fraction,
        "holdout_days": body.holdout_days,
        "purge_days": body.purge_days,
    }


async def _enqueue_job(
    db: AsyncSession,
    job: ResearchJob,
    job_type: str,
    payload: dict[str, Any],
) -> str | None:
    """Enqueue a research job to the arq queue.  Rolls back on failure."""
    try:
        pool = await get_redis_pool()
        queue_job_id = await enqueue_research(pool, str(job.id), job_type, payload)
        return queue_job_id
    except Exception as exc:
        await db.rollback()
        log.error("research_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue research job — Redis may be unavailable",
        ) from exc
