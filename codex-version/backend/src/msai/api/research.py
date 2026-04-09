from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.queue import enqueue_research_job, get_redis_pool, remove_queued_job
from msai.models import Strategy
from msai.schemas.research import (
    ComputeSlotUsage,
    ResearchCapacityResponse,
    ResearchCompareRequest,
    ResearchCompareResponse,
    ResearchJobControlResponse,
    ResearchJobDetail,
    ResearchJobRunResponse,
    ResearchJobSummary,
    ResearchPromotionRequest,
    ResearchPromotionResponse,
    ResearchReportDetail,
    ResearchReportSummary,
    ResearchSweepRunRequest,
    ResearchWalkForwardRunRequest,
    WorkerCapacitySummary,
)
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.research_artifacts import (
    ResearchArtifactNotFoundError,
    ResearchArtifactService,
    ResearchPromotionError,
)
from msai.services.research_jobs import ResearchJobNotFoundError, ResearchJobService
from msai.services.strategy_registry import StrategyRegistry
from msai.services.system_capacity import describe_system_capacity
from msai.services.user_identity import resolve_user_id_from_claims

router = APIRouter(prefix="/research", tags=["research"])
artifact_service = ResearchArtifactService()
job_service = ResearchJobService()


@router.get("/capacity", response_model=ResearchCapacityResponse)
async def get_research_capacity(
    _: Mapping[str, object] = Depends(get_current_user),
) -> ResearchCapacityResponse:
    pool = await get_redis_pool()
    snapshot = await describe_system_capacity(pool)
    return ResearchCapacityResponse(
        compute_slots=ComputeSlotUsage(**snapshot["compute_slots"]),
        workers=WorkerCapacitySummary(**snapshot["workers"]),
    )


@router.get("/jobs", response_model=list[ResearchJobSummary])
async def list_research_jobs(
    _: Mapping[str, object] = Depends(get_current_user),
    limit: int = 100,
) -> list[ResearchJobSummary]:
    bounded_limit = max(1, min(limit, 250))
    return [ResearchJobSummary(**job) for job in job_service.list_jobs(limit=bounded_limit)]


@router.get("/jobs/{job_id}", response_model=ResearchJobDetail)
async def get_research_job(
    job_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> ResearchJobDetail:
    try:
        job = job_service.load_job(job_id)
    except ResearchJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ResearchJobDetail(**job)


@router.post("/jobs/{job_id}/cancel", response_model=ResearchJobControlResponse)
async def cancel_research_job(
    job_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> ResearchJobControlResponse:
    try:
        job = job_service.load_job(job_id)
    except ResearchJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    status_value = str(job.get("status") or "pending")
    if status_value in {"completed", "failed", "cancelled"}:
        return ResearchJobControlResponse(
            job_id=job_id,
            status=status_value,
            progress_message=str(job.get("progress_message") or ""),
        )

    job = job_service.request_cancel(job_id)
    queue_job_id = job.get("queue_job_id")
    queue_name = str(job.get("queue_name") or settings.research_queue_name)
    if status_value == "pending" and isinstance(queue_job_id, str):
        pool = await get_redis_pool()
        await remove_queued_job(pool, queue_name=queue_name, queue_job_id=queue_job_id)
        job = job_service.mark_cancelled(job_id)

    return ResearchJobControlResponse(
        job_id=job_id,
        status=str(job.get("status") or "pending"),
        progress_message=str(job.get("progress_message") or ""),
    )


@router.post("/jobs/{job_id}/retry", response_model=ResearchJobRunResponse)
async def retry_research_job(
    job_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> ResearchJobRunResponse:
    try:
        job = job_service.load_job(job_id)
    except ResearchJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    status_value = str(job.get("status") or "pending")
    if status_value not in {"failed", "cancelled"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only failed or cancelled research jobs can be retried",
        )
    request_payload = dict(job.get("request") or {})
    job_type = str(job.get("job_type") or "")
    if job_type not in {"parameter_sweep", "walk_forward"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported research job type")
    return await _enqueue_research_job(job_type, request_payload)


@router.post("/sweeps", response_model=ResearchJobRunResponse)
async def run_parameter_sweep_job(
    payload: ResearchSweepRunRequest,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchJobRunResponse:
    request_payload = await _prepare_research_request(
        db,
        strategy_id=payload.strategy_id,
        instruments=payload.instruments,
        start_date=payload.start_date.isoformat(),
        end_date=payload.end_date.isoformat(),
        base_config=payload.base_config,
        parameter_grid=payload.parameter_grid,
        objective=payload.objective,
        max_parallelism=payload.max_parallelism,
        search_strategy=payload.search_strategy,
        study_name=payload.study_name,
        stage_fractions=payload.stage_fractions,
        reduction_factor=payload.reduction_factor,
        min_trades=payload.min_trades,
        require_positive_return=payload.require_positive_return,
        holdout_fraction=payload.holdout_fraction,
        holdout_days=payload.holdout_days,
        purge_days=payload.purge_days,
    )
    return await _enqueue_research_job("parameter_sweep", request_payload)


@router.post("/walk-forward", response_model=ResearchJobRunResponse)
async def run_walk_forward_job(
    payload: ResearchWalkForwardRunRequest,
    _: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchJobRunResponse:
    request_payload = await _prepare_research_request(
        db,
        strategy_id=payload.strategy_id,
        instruments=payload.instruments,
        start_date=payload.start_date.isoformat(),
        end_date=payload.end_date.isoformat(),
        base_config=payload.base_config,
        parameter_grid=payload.parameter_grid,
        objective=payload.objective,
        max_parallelism=payload.max_parallelism,
        search_strategy=payload.search_strategy,
        study_name=payload.study_name,
        stage_fractions=payload.stage_fractions,
        reduction_factor=payload.reduction_factor,
        min_trades=payload.min_trades,
        require_positive_return=payload.require_positive_return,
        holdout_fraction=payload.holdout_fraction,
        holdout_days=payload.holdout_days,
        purge_days=payload.purge_days,
    )
    request_payload["train_days"] = payload.train_days
    request_payload["test_days"] = payload.test_days
    request_payload["step_days"] = payload.step_days
    request_payload["mode"] = payload.mode
    return await _enqueue_research_job("walk_forward", request_payload)


@router.get("/reports", response_model=list[ResearchReportSummary])
async def list_research_reports(
    _: Mapping[str, object] = Depends(get_current_user),
    limit: int = 100,
) -> list[ResearchReportSummary]:
    bounded_limit = max(1, min(limit, 250))
    return artifact_service.list_reports(limit=bounded_limit)


@router.get("/reports/{report_id}", response_model=ResearchReportDetail)
async def get_research_report(
    report_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> ResearchReportDetail:
    try:
        report, summary = artifact_service.load_report_detail(report_id)
    except ResearchArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ResearchReportDetail(report=report, summary=summary)


@router.post("/compare", response_model=ResearchCompareResponse)
async def compare_research_reports(
    payload: ResearchCompareRequest,
    _: Mapping[str, object] = Depends(get_current_user),
) -> ResearchCompareResponse:
    reports: list[ResearchReportDetail] = []
    for report_id in payload.report_ids:
        try:
            report, summary = artifact_service.load_report_detail(report_id)
        except ResearchArtifactNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        reports.append(ResearchReportDetail(report=report, summary=summary))
    return ResearchCompareResponse(reports=reports)


@router.post("/promotions", response_model=ResearchPromotionResponse)
async def create_research_promotion(
    payload: ResearchPromotionRequest,
    claims: Mapping[str, object] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResearchPromotionResponse:
    try:
        report, _ = artifact_service.load_report_detail(payload.report_id)
        candidate = artifact_service.select_candidate(
            report,
            result_index=payload.result_index,
            window_index=payload.window_index,
        )
    except ResearchArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ResearchPromotionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    strategy = await _resolve_strategy_for_report(db, report.get("strategy_path"))
    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Strategy for research report is not registered",
        )

    user_id = await resolve_user_id_from_claims(db, claims)
    await db.commit()

    promotion = artifact_service.save_promotion(
        report_id=payload.report_id,
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        candidate=candidate,
        instruments=[str(value) for value in report.get("instruments", []) if value],
        created_by=user_id,
        paper_trading=payload.paper_trading,
    )
    return ResearchPromotionResponse(**promotion)


@router.get("/promotions/{promotion_id}", response_model=ResearchPromotionResponse)
async def get_research_promotion(
    promotion_id: str,
    _: Mapping[str, object] = Depends(get_current_user),
) -> ResearchPromotionResponse:
    try:
        promotion = artifact_service.load_promotion(promotion_id)
    except ResearchArtifactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ResearchPromotionResponse(**promotion)


async def _resolve_strategy_for_report(
    db: AsyncSession,
    strategy_path: object,
) -> Strategy | None:
    if strategy_path is None:
        return None

    registry = StrategyRegistry(settings.strategies_root)
    strategies = await registry.sync(db)
    requested_path = Path(str(strategy_path)).resolve()
    requested_name = Path(str(strategy_path)).with_suffix("").name
    requested_file = Path(str(strategy_path)).name

    for strategy in strategies:
        resolved = registry.resolve_path(strategy).resolve()
        if resolved == requested_path:
            return strategy
        if strategy.file_path == str(strategy_path):
            return strategy
        if strategy.name == requested_name:
            return strategy
        if Path(strategy.file_path).name == requested_file:
            return strategy
    return None


async def _prepare_research_request(
    db: AsyncSession,
    *,
    strategy_id: str,
    instruments: list[str],
    start_date: str,
    end_date: str,
    base_config: dict[str, object],
    parameter_grid: dict[str, list[object]],
    objective: str,
    max_parallelism: int | None,
    search_strategy: str,
    study_name: str | None,
    stage_fractions: list[float] | None,
    reduction_factor: int,
    min_trades: int | None,
    require_positive_return: bool,
    holdout_fraction: float | None,
    holdout_days: int | None,
    purge_days: int,
) -> dict[str, object]:
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")

    registry = StrategyRegistry(settings.strategies_root)
    strategy_path = registry.resolve_path(strategy)
    if not strategy_path.exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Strategy file not found: {strategy.file_path}",
        )

    try:
        canonical_instruments = await instrument_service.canonicalize_backtest_instruments(
            db,
            instruments,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return {
        "strategy_id": strategy.id,
        "strategy_name": strategy.name,
        "strategy_path": str(strategy_path),
        "instruments": canonical_instruments,
        "start_date": start_date,
        "end_date": end_date,
        "base_config": dict(base_config),
        "parameter_grid": {key: list(values) for key, values in parameter_grid.items()},
        "objective": objective,
        "max_parallelism": max_parallelism,
        "search_strategy": search_strategy,
        "study_name": study_name,
        "stage_fractions": list(stage_fractions) if stage_fractions else None,
        "reduction_factor": reduction_factor,
        "min_trades": min_trades,
        "require_positive_return": require_positive_return,
        "holdout_fraction": holdout_fraction,
        "holdout_days": holdout_days,
        "purge_days": purge_days,
    }


async def _enqueue_research_job(
    job_type: str,
    request_payload: dict[str, object],
) -> ResearchJobRunResponse:
    job = job_service.create_job(
        job_type=job_type,
        strategy_id=str(request_payload["strategy_id"]),
        strategy_name=str(request_payload["strategy_name"]),
        strategy_path=str(request_payload["strategy_path"]),
        request=request_payload,
    )

    try:
        pool = await get_redis_pool()
        queue_job_id = await enqueue_research_job(pool, job["id"], job_type, request_payload)
        job_service.mark_enqueued(
            job["id"],
            queue_name=settings.research_queue_name,
            queue_job_id=queue_job_id,
        )
    except Exception as exc:
        job_service.mark_failed(job["id"], error_message=f"Research queue unavailable: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Research queue unavailable",
        ) from exc

    return ResearchJobRunResponse(job_id=job["id"], status=job["status"])
