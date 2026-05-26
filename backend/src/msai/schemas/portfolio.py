"""Pydantic schemas for portfolio management API endpoints."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from msai.models.portfolio_enums import BacktestMode, PortfolioObjective, PortfolioRunStatus

# Legacy alias — pre-port rows stored the pre-rename spelling.  Translate
# on read so the API layer keeps working without a data migration.
_OBJECTIVE_LEGACY_ALIASES: dict[str, str] = {"max_sharpe": "maximize_sharpe"}


def _normalize_objective(raw: object) -> object:
    if isinstance(raw, str):
        return _OBJECTIVE_LEGACY_ALIASES.get(raw, raw)
    return raw


class PortfolioAllocationInput(BaseModel):
    """Input schema for a single allocation within a portfolio.

    ``weight`` is optional — when omitted, the portfolio service derives a
    heuristic weight from the candidate's metrics according to the portfolio
    ``objective`` (e.g. Sharpe-weighted for ``maximize_sharpe``).  A zero
    weight is rejected; use ``None`` (omit the field) to request heuristic
    derivation, or remove the allocation entirely to exclude the candidate.
    """

    candidate_id: UUID
    # ``gt=0.0`` disambiguates "no explicit weight" (None) from "exclude this
    # candidate" (which callers should encode by omitting the allocation).
    weight: float | None = Field(default=None, gt=0.0, le=1.0)


# Allocator names accepted by the portfolio backtest engine. Kept as a
# narrow string literal so OpenAPI / the typed frontend client both surface
# the exact set without a separate runtime enum (the DB column is a plain
# String(32), so introducing a Postgres ENUM would be churn for no win).
AllocatorName = Literal[
    "equal_weight",
    "fixed_weight",
    "inverse_vol",
    "vol_targeted",
]


class PortfolioCreate(BaseModel):
    """Request schema for creating a new portfolio.

    ``allocations`` is the legacy explicit-Candidate path: callers provide
    one ``PortfolioAllocationInput`` per graduated candidate. When omitted,
    the orchestration layer (F1c bridge) derives default Candidates from
    the portfolio's Strategy roster — Quick mode runs against those.

    Safety caps (``max_position_size``, ``max_drawdown_halt``) are in (0, 1]
    and represent fractions of base capital. ``default_mode`` selects
    Quick (single-shot) vs. Full (walk-forward) when ``/runs`` is called
    without an explicit override. ``allocator_name`` selects the weight
    rule applied by the engine (see ``AllocatorName``).
    """

    name: str = Field(max_length=128)
    description: str | None = None
    objective: PortfolioObjective
    base_capital: float = Field(gt=0.0)
    requested_leverage: float = Field(default=1.0, ge=0.1, le=10.0)
    # Downside-target is a risk-scaling cap; values <= 0 would silently pin
    # leverage to the safety floor and misrepresent intent.
    downside_target: float | None = Field(default=None, gt=0.0)
    benchmark_symbol: str | None = Field(default=None, max_length=32)
    # ``allocations`` is now optional: when omitted, the orchestration layer
    # derives default candidates from the portfolio's Strategy roster. When
    # provided, the existing min_length=1 guard still rejects empty lists at
    # the boundary (same UX as before for explicit-allocation callers).
    allocations: list[PortfolioAllocationInput] | None = Field(default=None, min_length=1)
    # F1c bridge: strategy-first compose path. When provided (and
    # ``allocations`` is None), the orchestration layer gets-or-creates a
    # default :class:`GraduationCandidate` per strategy. Mutually exclusive
    # with ``allocations`` (the ``_require_one_compose_path`` validator
    # enforces this at the boundary so the lifecycle code can branch
    # cleanly on which field is populated).
    strategy_ids: list[UUID] | None = Field(default=None, min_length=1)

    # B5: safety caps, mode, and allocator selection.
    max_position_size: float | None = Field(default=None, gt=0.0, le=1.0)
    max_drawdown_halt: float | None = Field(default=None, gt=0.0, le=1.0)
    default_mode: BacktestMode = BacktestMode.QUICK
    allocator_name: AllocatorName = "equal_weight"

    @field_validator("objective", mode="before")
    @classmethod
    def _translate_legacy_objective(cls, value: object) -> object:
        return _normalize_objective(value)

    @model_validator(mode="after")
    def _require_one_compose_path(self) -> PortfolioCreate:
        # Exactly one of ``strategy_ids`` (F1c bridge — auto-creates default
        # GraduationCandidates) or ``allocations`` (legacy explicit-candidate
        # path) must be provided. Allowing both would create two parallel
        # truths for what the portfolio is composed of; allowing neither
        # would leave the portfolio with zero members and a
        # PortfolioOrchestrationError on the first run.
        if not self.strategy_ids and not self.allocations:
            raise ValueError("either strategy_ids or allocations is required")
        if self.strategy_ids and self.allocations:
            raise ValueError("provide strategy_ids OR allocations, not both")
        return self

    @model_validator(mode="after")
    def _full_mode_rejects_fixed_weight(self) -> PortfolioCreate:
        # Full mode is the walk-forward + Optuna search path; the allocator
        # is consumed by the per-trial returns aggregator and must compute
        # weights from the candidate / returns matrix (equal_weight,
        # inverse_vol, vol_targeted). ``fixed_weight`` requires
        # operator-supplied weights at construction time and cannot be
        # auto-derived per trial — silently degrading to equal-weight
        # (the old fallback in ``_aggregate_returns_trial``) would lie
        # about what the optimizer actually fit. Reject at the boundary
        # so the caller learns immediately.
        if self.default_mode is BacktestMode.FULL and self.allocator_name == "fixed_weight":
            raise ValueError(
                "Full mode requires an allocator that auto-computes weights "
                "(equal_weight / inverse_vol / vol_targeted); fixed_weight is "
                "operator-specified and not suitable for optimization"
            )
        return self

    @model_validator(mode="after")
    def _manual_objective_requires_explicit_weights(self) -> PortfolioCreate:
        # ``objective="manual"`` promises the operator will set each
        # weight.  If any allocation omits the weight, the service layer
        # silently falls through to equal-weight heuristic — not what
        # the caller asked for.  Fail at the boundary so the caller
        # learns about the mismatch immediately.
        #
        # Skip when ``allocations`` is None: the F1c bridge will derive
        # candidates from Strategies; manual-weight semantics don't apply
        # because there is no per-allocation weight to omit.
        if self.objective is PortfolioObjective.MANUAL and self.allocations is not None:
            missing = [
                str(alloc.candidate_id) for alloc in self.allocations if alloc.weight is None
            ]
            if missing:
                raise ValueError(
                    "objective=manual requires an explicit weight on every "
                    f"allocation; missing weight for candidates: {missing}"
                )
        return self


class PortfolioResponse(BaseModel):
    """Response schema for a single portfolio."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    objective: PortfolioObjective
    base_capital: float
    requested_leverage: float
    downside_target: float | None
    benchmark_symbol: str | None
    account_id: str | None
    # B5: safety caps + mode + allocator surfaced on read so the UI can
    # show what the portfolio was configured with.
    max_position_size: float | None = None
    max_drawdown_halt: float | None = None
    default_mode: BacktestMode = BacktestMode.QUICK
    allocator_name: str = "equal_weight"
    created_at: datetime
    updated_at: datetime

    @field_validator("objective", mode="before")
    @classmethod
    def _translate_legacy_objective(cls, value: object) -> object:
        # Translate pre-rename "max_sharpe" rows transparently — strict
        # enum validation would otherwise 500 on GET for legacy data.
        return _normalize_objective(value)


class PortfolioListResponse(BaseModel):
    """Paginated list response for portfolios."""

    items: list[PortfolioResponse]
    total: int


class PortfolioAllocationResponse(BaseModel):
    """Response schema for a single ``PortfolioAllocation`` row.

    Surfaced via ``GET /api/v1/portfolios/{portfolio_id}/allocations`` so the
    UI and the F1c bridge regression tests can verify which candidates are
    bound to a portfolio after compose (the candidate id is the bridge's
    idempotency key — repeat compose with the same strategy must return
    the same candidate id).

    NB: :class:`PortfolioAllocation` only carries ``created_at`` — there is
    no ``updated_at`` column on the row (allocations are immutable after
    compose; rebalancing creates a new revision instead).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    portfolio_id: UUID
    candidate_id: UUID
    weight: float | None
    created_at: datetime


# Minimum inclusive-day range for a Full-mode run.  The orchestrator's
# adaptive walk-forward scaler (``_scaled_walk_forward_params``) floors
# each leg at 30 days; a 90-day range is the smallest input that yields
# at least one walk-forward window after scaling (60d train + 30d test).
# Anything shorter would raise ``ValueError`` inside the worker and
# leave the caller with a generic 500 instead of a precise 422.
_FULL_MODE_MIN_RANGE_DAYS = 90


class PortfolioRunCreate(BaseModel):
    """Request schema for launching a portfolio backtest run.

    ``portfolio_id`` is optional on the body — the standard route at
    ``POST /portfolios/{portfolio_id}/runs`` takes it from the URL. It is
    accepted in the body for unified-API callers that prefer a single
    request shape.
    """

    portfolio_id: UUID | None = None
    start_date: date
    end_date: date
    # Bounded to guard against runaway thread-pool sizes; the worker clamps
    # further via ``compute_slots`` global limits.
    max_parallelism: int | None = Field(default=None, ge=1, le=32)
    # B5: Quick (single-shot) vs. Full (walk-forward + Optuna optimization).
    # ``None`` means "inherit from ``Portfolio.default_mode``" — the lifecycle
    # service resolves this at persistence time.  Defaulting to ``QUICK`` here
    # would silently override the portfolio's ``default_mode='full'`` whenever
    # a client omits the field (codex-bot P2 finding on PR #73).
    mode: BacktestMode | None = None
    # Optional Full-mode trial-count override.  Production runs use the
    # ``settings.portfolio_full_trial_count`` default (~100).  Test/explore
    # mode: set to a small integer (2-10) to keep a smoke run inside a
    # verify-e2e time budget; the FAIL_STALE classification from the first
    # E2E pass on portfolio-backtest blocked UC-PB-API-003 (Full smoke)
    # because there was no API-level way to cap trials.  Only honored when
    # ``mode == BacktestMode.FULL``; Quick mode ignores it.
    n_trials: int | None = Field(default=None, ge=1, le=1000)
    # Operational smoke-test marker. Defaults to False so existing callers
    # (UI compose, /backtests/history, all PR-#73 tests) stay unaffected.
    # When True, the worker's metrics-emit path enriches ``run.metrics``
    # with the G5 block (pnl / trade_count_by_strategy / trade_count_total
    # / benchmark_symbol / smoke_config) — see
    # ``services/portfolio/orchestration.py::enrich_smoke_metrics``.
    # PRD docs/prds/ingest-backtest-smoke-test.md v1.3.
    smoke: bool = False

    @model_validator(mode="after")
    def _validate_date_range(self) -> PortfolioRunCreate:
        # Codex bot iter-14 P2 on PR #73: reject reversed ranges
        # (``end_date < start_date``) at the schema layer instead of
        # letting the worker fail with a less actionable error after
        # consuming queue/compute capacity. Applies to BOTH modes since
        # an inverted range is meaningless regardless of mode.
        if self.end_date < self.start_date:
            raise ValueError(
                f"end_date ({self.end_date}) must be >= start_date ({self.start_date})"
            )

        # Walk-forward analysis needs enough headroom for at least one
        # IS+OOS pair after the orchestrator's adaptive scaling
        # (``_scaled_walk_forward_params`` floors at 30 days/leg).  A
        # too-short Full range would raise ``ValueError("No walk-forward
        # windows fit ...")`` inside the worker; rejecting here keeps the
        # error precise (422 with a helpful message) and avoids spending
        # the work of enqueuing a job that is guaranteed to fail.
        # Quick mode is single-shot — any inclusive range is valid.
        # When ``mode`` is None the lifecycle service inherits from the
        # portfolio's ``default_mode`` — that resolution happens at persist
        # time so we cannot reject here.  Validators only fire on an
        # explicit ``mode=FULL``; the equivalent guard for inherited Full
        # runs lives in ``PortfolioLifecycle.create_run``.
        if self.mode is BacktestMode.FULL:
            range_days = (self.end_date - self.start_date).days + 1
            if range_days < _FULL_MODE_MIN_RANGE_DAYS:
                raise ValueError(
                    f"Full mode requires at least {_FULL_MODE_MIN_RANGE_DAYS} days "
                    f"between start_date and end_date (got {range_days} day"
                    f"{'s' if range_days != 1 else ''}); use mode='quick' for "
                    "shorter ranges or extend the window."
                )
        return self


class PortfolioRunResponse(BaseModel):
    """Response schema for a single portfolio backtest run."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    portfolio_id: UUID
    status: PortfolioRunStatus
    metrics: dict[str, Any] | None
    series: list[dict[str, Any]] | None
    allocations: list[dict[str, Any]] | None
    report_path: str | None
    start_date: date
    end_date: date
    max_parallelism: int | None
    error_message: str | None
    heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    # B5: optimizer trace + IS/OOS metrics + mode surfaced on read so the
    # results UI (per-strategy heatmap, IS/OOS panel, trials chart) and the
    # Promote-to-Live gating logic both work off the persisted shape.
    mode: BacktestMode = BacktestMode.QUICK
    optimization_trace: list[dict[str, Any]] | None = None
    walk_forward_payload: dict[str, Any] | None = None
    is_metric: float | None = None
    oos_metric: float | None = None
    # Operational smoke-test marker. Mirrors ``PortfolioRunCreate.smoke``;
    # defaults to False for non-smoke runs. Distinguishes operator-driven
    # canonical smoke runs (per ``/api/v1/backtests/history?smoke_only=true``)
    # from ordinary portfolio backtests. PRD v1.3.
    smoke: bool = False


class PortfolioRunListResponse(BaseModel):
    """Paginated list response for portfolio runs."""

    items: list[PortfolioRunResponse]
    total: int
