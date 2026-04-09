from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

ResearchJobType = Literal["parameter_sweep", "walk_forward"]
ResearchSearchStrategy = Literal["auto", "grid", "successive_halving", "regime_halving", "optuna"]


class ResearchReportSummary(BaseModel):
    id: str
    mode: str
    generated_at: str | None = None
    strategy_path: str | None = None
    strategy_name: str | None = None
    instruments: list[str] = Field(default_factory=list)
    objective: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    best_config: dict[str, Any] | None = None
    best_metrics: dict[str, Any] | None = None
    candidate_count: int = 0


class ResearchReportDetail(BaseModel):
    report: dict[str, Any]
    summary: ResearchReportSummary


class ResearchCompareRequest(BaseModel):
    report_ids: list[str] = Field(min_length=2, max_length=3)


class ResearchCompareResponse(BaseModel):
    reports: list[ResearchReportDetail]


class ResearchPromotionRequest(BaseModel):
    report_id: str
    result_index: int | None = None
    window_index: int | None = None
    paper_trading: bool = True


class ResearchPromotionResponse(BaseModel):
    id: str
    report_id: str
    created_at: str
    created_by: str | None = None
    paper_trading: bool = True
    strategy_id: str
    strategy_name: str
    instruments: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    selection: dict[str, Any] = Field(default_factory=dict)
    live_url: str


class ResearchRunBase(BaseModel):
    strategy_id: str
    instruments: list[str] = Field(min_length=1)
    start_date: date
    end_date: date
    base_config: dict[str, Any] = Field(default_factory=dict)
    parameter_grid: dict[str, list[Any]] = Field(default_factory=dict)
    objective: str = "sharpe"
    max_parallelism: int | None = Field(default=None, ge=1, le=32)
    search_strategy: ResearchSearchStrategy = "auto"
    study_name: str | None = Field(default=None, min_length=1, max_length=120)
    stage_fractions: list[float] | None = None
    reduction_factor: int = Field(default=2, ge=2, le=8)
    min_trades: int | None = Field(default=None, ge=1)
    require_positive_return: bool = False
    holdout_fraction: float | None = Field(default=None, gt=0.0, lt=0.5)
    holdout_days: int | None = Field(default=None, ge=1)
    purge_days: int = Field(default=5, ge=0, le=60)


class ResearchSweepRunRequest(ResearchRunBase):
    pass


class ResearchWalkForwardRunRequest(ResearchRunBase):
    train_days: int = Field(ge=1)
    test_days: int = Field(ge=1)
    step_days: int | None = Field(default=None, ge=1)
    mode: Literal["rolling", "expanding"] = "rolling"


class ResearchJobSummary(BaseModel):
    id: str
    job_type: ResearchJobType
    status: str
    progress: int = 0
    progress_message: str | None = None
    stage_index: int | None = None
    stage_count: int | None = None
    completed_trials: int | None = None
    total_trials: int | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    heartbeat_at: str | None = None
    error_message: str | None = None
    report_id: str | None = None
    queue_name: str | None = None
    queue_job_id: str | None = None
    worker_id: str | None = None
    attempt: int = 0
    cancel_requested: bool = False
    strategy_id: str
    strategy_name: str
    strategy_path: str
    instruments: list[str] = Field(default_factory=list)
    objective: str | None = None


class ResearchJobDetail(ResearchJobSummary):
    request: dict[str, Any] = Field(default_factory=dict)
    report_summary: ResearchReportSummary | None = None


class ResearchJobRunResponse(BaseModel):
    job_id: str
    status: str


class ResearchJobControlResponse(BaseModel):
    job_id: str
    status: str
    progress_message: str | None = None


class ComputeSlotLease(BaseModel):
    lease_id: str
    job_kind: str
    job_id: str
    slot_count: int
    updated_at: str | None = None


class ComputeSlotUsage(BaseModel):
    limit: int
    used: int
    available: int
    active_leases: int
    leases: list[ComputeSlotLease] = Field(default_factory=list)


class WorkerInstance(BaseModel):
    worker_id: str
    worker_role: str
    queue_name: str
    max_jobs: int
    hostname: str | None = None
    pid: int | None = None
    started_at: str | None = None
    updated_at: str | None = None


class QueueCapacity(BaseModel):
    queue_name: str
    worker_role: str
    active_workers: int
    total_capacity: int
    max_jobs_per_worker: int
    queued_jobs: int = 0


class WorkerCapacitySummary(BaseModel):
    total_active_workers: int
    total_capacity: int
    workers: list[WorkerInstance] = Field(default_factory=list)
    queues: list[QueueCapacity] = Field(default_factory=list)


class ResearchCapacityResponse(BaseModel):
    compute_slots: ComputeSlotUsage
    workers: WorkerCapacitySummary
