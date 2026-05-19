"""Portfolio management API router -- create, list, and run portfolio backtests.

Manages portfolios of graduated strategies with weighted capital allocation
and combined backtest runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID  # noqa: TC003 -- FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


from msai.core.auth import get_current_user, resolve_user_id
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.core.queue import enqueue_portfolio_run, get_redis_pool
from msai.schemas.portfolio import (
    PortfolioAllocationResponse,
    PortfolioCreate,
    PortfolioListResponse,
    PortfolioResponse,
    PortfolioRunCreate,
    PortfolioRunListResponse,
    PortfolioRunResponse,
)
from msai.services.portfolio import PortfolioService

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/portfolios", tags=["portfolios"])

# Module-level singleton -- stateless service, safe to share.
_service = PortfolioService()


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios -- list portfolios
# ---------------------------------------------------------------------------


@router.get("", response_model=PortfolioListResponse)
async def list_portfolios(
    limit: int = Query(default=100, ge=1, le=500),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioListResponse:
    """List portfolios ordered by creation time (newest first)."""
    portfolios = await _service.list(db, limit=limit)
    total = await _service.count(db)

    return PortfolioListResponse(
        items=[PortfolioResponse.model_validate(p) for p in portfolios],
        total=total,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/portfolios -- create a portfolio
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=PortfolioResponse,
)
async def create_portfolio(
    body: PortfolioCreate,
    response: Response,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioResponse:
    """Create a new portfolio with weighted strategy allocations.

    Returns 201 + the created :class:`PortfolioResponse` and a
    ``Location: /api/v1/portfolios/{id}`` header (per ``api-design.md``
    rule 4 — all POST creates must include a Location header pointing at
    the new resource so clients can navigate without parsing the response
    body).
    """
    user_id = await resolve_user_id(db, claims)
    try:
        portfolio = await _service.create(db, body, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    await db.commit()
    await db.refresh(portfolio)

    response.headers["Location"] = f"/api/v1/portfolios/{portfolio.id}"
    return PortfolioResponse.model_validate(portfolio)


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/runs -- list all portfolio runs
# IMPORTANT: Static /runs routes MUST be registered before /{portfolio_id}
# to avoid FastAPI matching "runs" as a UUID path parameter.
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=PortfolioRunListResponse)
async def list_portfolio_runs(
    portfolio_id: UUID | None = Query(default=None, description="Filter by portfolio"),  # noqa: B008
    limit: int = Query(default=100, ge=1, le=500),  # noqa: B008
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioRunListResponse:
    """List portfolio runs, optionally filtered by portfolio ID."""
    runs = await _service.list_runs(db, portfolio_id=portfolio_id, limit=limit)
    total = await _service.count_runs(db, portfolio_id=portfolio_id)

    return PortfolioRunListResponse(
        items=[PortfolioRunResponse.model_validate(r) for r in runs],
        total=total,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/runs/{run_id} -- run detail
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}", response_model=PortfolioRunResponse)
async def get_portfolio_run(
    run_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioRunResponse:
    """Return a single portfolio run by ID."""
    try:
        run = await _service.get_run(db, run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio run {run_id} not found",
        ) from None
    return PortfolioRunResponse.model_validate(run)


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/runs/{run_id}/report -- download HTML report
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/report")
async def get_portfolio_run_report(
    run_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> FileResponse:
    """Download the generated HTML report for a completed portfolio run."""
    try:
        run = await _service.get_run(db, run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio run {run_id} not found",
        ) from None

    if run.report_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No report available for portfolio run {run_id}",
        )

    # Path traversal protection: ensure resolved path is within expected
    # directory.  ``Path.is_relative_to`` rejects prefix-collision shenanigans
    # like ``/data/reports-evil/...`` that a plain ``startswith`` would let
    # through (same pattern as ``api/backtests.py:924``).
    report_file = Path(run.report_path).resolve()
    expected_dir = (Path(settings.data_root) / "reports").resolve()
    if not report_file.is_relative_to(expected_dir):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid report path",
        )

    if not report_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report file not found on disk for portfolio run {run_id}",
        )

    return FileResponse(
        path=str(report_file),
        media_type="text/html",
        filename=f"portfolio_run_{run_id}_report.html",
    )


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/{portfolio_id} -- portfolio detail
# ---------------------------------------------------------------------------


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
async def get_portfolio(
    portfolio_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioResponse:
    """Return a single portfolio by ID."""
    try:
        portfolio = await _service.get(db, portfolio_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio {portfolio_id} not found",
        ) from None
    return PortfolioResponse.model_validate(portfolio)


# ---------------------------------------------------------------------------
# GET /api/v1/portfolios/{portfolio_id}/allocations -- list allocations
#
# Surfaces the candidate roster bound to a portfolio (the F1c bridge's
# idempotency key — repeat compose with the same strategy reuses the same
# candidate id, which the regression suite verifies via this endpoint).
# ---------------------------------------------------------------------------


@router.get(
    "/{portfolio_id}/allocations",
    response_model=list[PortfolioAllocationResponse],
)
async def list_portfolio_allocations(
    portfolio_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> list[PortfolioAllocationResponse]:
    """List allocations (candidate + weight) for a portfolio."""
    # Validate portfolio exists -- otherwise we'd silently return [] for a
    # bogus id and mask a 404 that the operator should see.
    try:
        await _service.get(db, portfolio_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio {portfolio_id} not found",
        ) from None
    allocations = await _service.get_allocations(db, portfolio_id)
    return [PortfolioAllocationResponse.model_validate(a) for a in allocations]


# ---------------------------------------------------------------------------
# POST /api/v1/portfolios/{portfolio_id}/runs -- start a portfolio run
# ---------------------------------------------------------------------------


@router.post(
    "/{portfolio_id}/runs",
    status_code=status.HTTP_201_CREATED,
    response_model=PortfolioRunResponse,
)
async def create_portfolio_run(
    portfolio_id: UUID,
    body: PortfolioRunCreate,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioRunResponse:
    """Create a portfolio backtest run and enqueue it for execution."""
    user_id = await resolve_user_id(db, claims)

    # Cross-validate the per-run mode against the portfolio's allocator.
    # ``PortfolioCreate`` already rejects ``default_mode=full`` paired with
    # ``allocator_name=fixed_weight`` at compose time, but a Quick-default
    # portfolio with ``fixed_weight`` could still be launched in Full via
    # the per-run override. Reject here so the orchestration layer never
    # silently degrades fixed_weight to equal_weight during Full-mode trials
    # (see ``_aggregate_returns_trial`` rationale).
    from msai.models.portfolio_enums import BacktestMode  # local-import per memory  # noqa: PLC0415

    try:
        existing_portfolio = await _service.get(db, portfolio_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio {portfolio_id} not found",
        ) from None

    if (
        body.mode is BacktestMode.FULL
        and (existing_portfolio.allocator_name or "equal_weight") == "fixed_weight"
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Full mode requires an allocator that auto-computes weights "
                "(equal_weight / inverse_vol / vol_targeted); this portfolio uses "
                "fixed_weight, which is operator-specified and not suitable for "
                "optimization. Either run Quick mode or rebuild the portfolio with "
                "a non-fixed allocator."
            ),
        )

    try:
        run = await _service.create_run(db, portfolio_id, body, user_id=user_id)
    except ValueError as exc:
        # Portfolio existence was already verified above (line ~284). Any
        # ValueError raised by ``create_run`` at this point is a validation
        # failure (e.g. the 90-day minimum when ``mode=full`` is inherited
        # from ``portfolio.default_mode``), not a missing-row error. Surface
        # the actionable message at 422 so the UI/CLI tells the user how to
        # fix the request rather than misleadingly claiming the portfolio
        # doesn't exist. Codex bot iter-3 P2 on PR #73.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # Enqueue to arq BEFORE commit -- if enqueue fails, rollback the row
    try:
        pool = await get_redis_pool()
        await enqueue_portfolio_run(pool, str(run.id), str(portfolio_id))
    except Exception as exc:
        await db.rollback()
        log.error("portfolio_run_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue portfolio run job -- Redis may be unavailable",
        ) from exc

    await db.commit()
    await db.refresh(run)

    log.info(
        "portfolio_run_enqueued",
        run_id=str(run.id),
        portfolio_id=str(portfolio_id),
    )
    return PortfolioRunResponse.model_validate(run)


# ---------------------------------------------------------------------------
# G1: POST /api/v1/portfolios/runs/{run_id}/cancel
#
# Imports kept module-local so the PostToolUse ruff formatter (which strips
# "unused" imports) keeps them — Edit calls that add an import without an
# immediate consumer get the import removed before the next call lands. See
# ``feedback_colocate_imports_with_usage_in_edits.md``.
# ---------------------------------------------------------------------------
from pydantic import BaseModel, Field  # noqa: E402

from msai.models.portfolio_enums import PortfolioRunStatus  # noqa: E402
from msai.services.live.portfolio_service import (  # noqa: E402
    PortfolioService as LivePortfolioService,
)
from msai.services.portfolio.lifecycle import PortfolioLifecycle  # noqa: E402
from msai.services.risk_engine import RiskEngine  # noqa: E402


class PromoteToLiveBody(BaseModel):
    """Request body for ``POST /portfolios/runs/{id}/promote-to-live``.

    ``account_id`` is the IB account the promoted portfolio will trade
    against. Phase 1 paper-only enforcement: account ids MUST start with
    ``DU``; anything else is rejected with 422 so the promote path can
    never silently route a backtest into a real-money account.
    """

    account_id: str = Field(min_length=3, max_length=64)


class PromoteToLiveResponse(BaseModel):
    """Response body for the promote-to-live endpoint.

    Carries enough revision metadata for the frontend to mount
    ``PortfolioStartDialog`` directly without a follow-up fetch. Codex-bot
    PR-73 P2 caught the UX dead-end where the promote handler redirected
    to ``/live-trading?revision=<id>`` but no page consumed the query and
    the dialog wasn't mounted; the user could not actually start the
    deployment after a successful promote.
    """

    live_portfolio_id: UUID
    live_portfolio_revision_id: UUID
    revision_number: int
    composition_hash: str


@router.post(
    "/runs/{run_id}/cancel",
    status_code=status.HTTP_200_OK,
    response_model=PortfolioRunResponse,
)
async def cancel_portfolio_run(
    run_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PortfolioRunResponse:
    """Cancel a non-terminal :class:`PortfolioRun`.

    - 200 + the updated :class:`PortfolioRunResponse` on success.
    - 404 if the run id does not exist.
    - 409 if the run is already in a terminal state
      (``completed`` / ``failed`` / ``canceled``).

    The worker's status-transition guards
    (:meth:`PortfolioLifecycle.mark_run_running`) refuse to lift a run out
    of a terminal state, so flipping the status here is safe even under
    an arq retry: if the worker tries to mark this row ``running`` after
    we set it ``canceled``, it raises ``PortfolioRunTerminalStateError``
    and aborts cleanly.
    """
    # Codex bot iter-11 P1 on PR #73: cancel via an atomic conditional
    # UPDATE that only succeeds from non-terminal states. The previous
    # read-then-modify-then-commit pattern had a TOCTOU race with the
    # worker's completion write — whichever transaction commits last
    # could clobber the other, so a finished run could end up marked
    # ``canceled`` (or an operator cancel could be lost).
    #
    # ``UPDATE ... WHERE status IN ('pending', 'running')`` only fires
    # when the row is still in a non-terminal state at write time;
    # rowcount==0 means the worker beat us to a terminal state (or the
    # row doesn't exist), and we surface that as 404/409 based on a
    # follow-up read.
    from sqlalchemy import update as sql_update  # noqa: PLC0415

    from msai.models.portfolio_run import PortfolioRun as _PortfolioRunModel  # noqa: PLC0415

    non_terminal_statuses = [
        PortfolioRunStatus.PENDING.value,
        PortfolioRunStatus.RUNNING.value,
    ]
    result = await db.execute(
        sql_update(_PortfolioRunModel)
        .where(
            _PortfolioRunModel.id == run_id,
            _PortfolioRunModel.status.in_(non_terminal_statuses),
        )
        .values(status=PortfolioRunStatus.CANCELED.value)
    )
    # ``session.execute`` on a Core UPDATE statement returns a
    # ``CursorResult`` at runtime; SQLAlchemy's static type is the
    # base ``Result[Any]`` which mypy doesn't see ``rowcount`` on.
    # Cast to surface the attribute.
    rowcount = int(cast("CursorResult[Any]", result).rowcount or 0)
    await db.commit()

    if rowcount == 0:
        # Atomic UPDATE didn't fire — either the row is missing or it's
        # already terminal. Distinguish via a fresh read so the operator
        # sees the right HTTP code.
        try:
            existing = await PortfolioLifecycle.get_run(db, run_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Portfolio run {run_id} not found",
            ) from None
        existing_status = PortfolioRunStatus(existing.status)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(f"Portfolio run {run_id} is {existing_status.value}; cannot cancel"),
        )

    run = await PortfolioLifecycle.get_run(db, run_id)
    log.info(
        "portfolio_run_canceled",
        run_id=str(run_id),
    )
    return PortfolioRunResponse.model_validate(run)


# ---------------------------------------------------------------------------
# G2: POST /api/v1/portfolios/runs/{run_id}/promote-to-live
#
# Risk-engine-gated promotion of a completed backtest into a frozen
# LivePortfolio + LivePortfolioRevision. Phase 1 paper-only enforcement
# (account_id must start with ``DU``).
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/promote-to-live",
    status_code=status.HTTP_201_CREATED,
    response_model=PromoteToLiveResponse,
)
async def promote_portfolio_run_to_live(
    run_id: UUID,
    body: PromoteToLiveBody,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> PromoteToLiveResponse:
    """Promote a completed :class:`PortfolioRun` to a live paper portfolio.

    Rules:

    - 404 if the run id does not exist.
    - 422 if the run is not in ``completed`` status.
    - 422 if ``account_id`` does not start with ``DU`` (paper-only).
    - 422 if the RiskEngine validates the resulting composition and rejects.

    On success returns 201 + the new ``live_portfolio_id`` /
    ``live_portfolio_revision_id`` so the caller can immediately POST to
    ``/api/v1/live/start-portfolio`` with the new revision id.
    """
    # Paper-only enforcement (Phase 1). The schema's ``min_length`` already
    # rejects an empty string; the prefix check rejects "U..." live ids and
    # garbage values like "DEMO-1" that don't start with the IB paper-account
    # convention. ``starts-with`` is the only check at this layer — the live
    # deploy path re-validates against ``IB_ACCOUNT_ID`` env at start time.
    if not body.account_id.startswith("DU"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": {
                    "code": "PAPER_ONLY_ENFORCED",
                    "message": (
                        "Phase 1 promote-to-live is paper-only: "
                        f"account_id must start with 'DU' (got {body.account_id!r})"
                    ),
                }
            },
        )

    # Load the run + portfolio. Both are required for the materialization
    # step; missing either is a 404 (run) or a 422 (portfolio FK violation,
    # which only happens for corrupted DB state).
    try:
        run = await PortfolioLifecycle.get_run(db, run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio run {run_id} not found",
        ) from None

    if run.status != PortfolioRunStatus.COMPLETED.value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": {
                    "code": "RUN_NOT_COMPLETED",
                    "message": (
                        f"Cannot promote a {run.status} run; promotion requires "
                        f"status='completed' (got '{run.status}')"
                    ),
                }
            },
        )

    try:
        portfolio = await PortfolioLifecycle.get(db, run.portfolio_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Source portfolio {run.portfolio_id} not found",
        ) from None

    # Risk-engine validation. The RiskEngine's defaults
    # (``max_position_size=10_000``, ``max_notional_exposure=500_000``)
    # are the platform-wide live caps; we instantiate a default engine
    # here rather than reuse the live router's module-level singleton so
    # promote-to-live can't accidentally inherit a halted state from
    # a prior live deploy.
    requested_leverage = float(portfolio.requested_leverage or 1.0)
    base_capital = float(portfolio.base_capital or 0.0)
    risk_engine = RiskEngine()
    notional = base_capital * requested_leverage
    if not risk_engine.check_notional_exposure(notional):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": {
                    "code": "RISK_VALIDATION_FAILED",
                    "message": (
                        f"Risk-engine rejected promotion: requested leverage "
                        f"{requested_leverage}x against base capital "
                        f"${base_capital:,.0f} produces notional "
                        f"${notional:,.0f} > max "
                        f"${risk_engine.limits.max_notional_exposure:,.0f}. "
                        "Reduce leverage or split into multiple portfolios."
                    ),
                }
            },
        )

    user_id = await resolve_user_id(db, claims)

    live_svc = LivePortfolioService(db)
    try:
        live_portfolio, revision = await live_svc.materialize_from_backtest(
            portfolio=portfolio,
            run=run,
            account_id=body.account_id,
            created_by=user_id,
        )
    except Exception as exc:  # noqa: BLE001 — surface domain errors verbatim
        await db.rollback()
        log.error(
            "portfolio_promote_to_live_failed",
            run_id=str(run_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": {
                    "code": "MATERIALIZATION_FAILED",
                    "message": str(exc),
                }
            },
        ) from exc

    await db.commit()
    await db.refresh(live_portfolio)
    await db.refresh(revision)

    log.info(
        "portfolio_promoted_to_live",
        run_id=str(run_id),
        live_portfolio_id=str(live_portfolio.id),
        live_portfolio_revision_id=str(revision.id),
        account_id=body.account_id,
    )
    return PromoteToLiveResponse(
        live_portfolio_id=live_portfolio.id,
        live_portfolio_revision_id=revision.id,
        revision_number=revision.revision_number,
        composition_hash=revision.composition_hash,
    )
