"""FastAPI endpoints for Symbol Onboarding.

Routes:
- POST /api/v1/symbols/onboard/dry-run — Preflight cost estimate (no DB write).
- POST /api/v1/symbols/onboard — Start onboarding job (async, returns 202 + job_id).
- GET /api/v1/symbols/onboard/{run_id}/status — Poll job progress.
- POST /api/v1/symbols/onboard/{run_id}/repair — Retry failed symbols only.
- GET /api/v1/symbols/readiness — Window-scoped per-instrument readiness.
"""

from __future__ import annotations

import asyncio
from datetime import date as _date
from decimal import Decimal
from pathlib import Path as _FsPath
from typing import Any, cast
from uuid import UUID, uuid4

import redis.exceptions as redis_exceptions
import structlog
from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from msai.api._common import error_response
from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.queue import get_redis_pool
from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.schemas.symbol_onboarding import (
    DryRunResponse,
    OnboardProgress,
    OnboardRequest,
    OnboardResponse,
    ReadinessResponse,
    RunStatus,
    StatusResponse,
    SymbolStateRow,
    SymbolStatus,
)
from msai.services.nautilus.security_master.registry import AmbiguousSymbolError
from msai.services.nautilus.security_master.service import (
    SecurityMaster,
    compute_blake2b_digest_key,
)
from msai.services.symbol_onboarding import normalize_asset_class_for_ingest
from msai.services.symbol_onboarding.cost_estimator import (
    estimate_cost,
)
from msai.services.symbol_onboarding.coverage import compute_coverage
from msai.services.symbol_onboarding.manifest import ParsedManifest

# Narrow exception set treated as "queue unavailable" (HTTP 503). Anything
# outside this set propagates as a 500 so programmer errors don't silently
# masquerade as transient infra outages.
_QUEUE_UNAVAILABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    redis_exceptions.RedisError,
    ConnectionError,
    OSError,
    TimeoutError,
)

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/symbols", tags=["symbols"])

_DATABENTO_CLIENT_CACHE: Any = None


def _get_databento_client() -> Any:
    """Lazy module-level singleton for the Databento SDK Historical client.

    The cost estimator calls ``client.metadata.get_cost(...)`` on this object,
    which is provided by the underlying ``databento.Historical`` SDK class
    (NOT MSAI's ``DatabentoClient`` wrapper, which only proxies ``timeseries``
    + ``definition`` paths). Tests can monkeypatch this seam to inject a fake.
    """
    global _DATABENTO_CLIENT_CACHE
    if _DATABENTO_CLIENT_CACHE is None:
        import databento as db

        from msai.core.config import settings

        if not settings.databento_api_key:
            raise RuntimeError("DATABENTO_API_KEY is not configured")
        _DATABENTO_CLIENT_CACHE = db.Historical(key=settings.databento_api_key)
    return _DATABENTO_CLIENT_CACHE


@router.post("/onboard/dry-run", response_model=DryRunResponse)
async def onboard_dry_run(
    request: OnboardRequest,
    _user: Any = Depends(get_current_user),  # noqa: B008
) -> DryRunResponse:
    """Pure preflight — no DB write, no job enqueue.

    Returns Databento cost estimate for the batch. Does NOT check live
    qualification readiness or trigger any ingest jobs.

    **Request contract:**
    - ``watchlist_name`` — kebab-case identifier (for grouping + future UI).
    - ``symbols[*].asset_class`` — must be one of: equity | futures | fx | option.
    - ``symbols[*].start/end`` — date window (must be <= today).
    - ``cost_ceiling_usd`` — optional hard spend cap (for operator guardrail).
    - ``request_live_qualification`` — if True, cost estimate includes IB
      refresh cost (currently not priced separately; deferred to Phase 2).

    **Response contract:**
    - ``dry_run: True`` — constant marker.
    - ``estimate_confidence`` — one of: high (end < today-1d) | medium | low.
    - ``breakdown[]`` — per-symbol line items.

    **Errors:**
    - 422 Unprocessable Entity — invalid schema or >100 symbols.
    - 401 Unauthorized — JWT missing or invalid.
    """
    estimate = await _compute_cost_estimate(request)
    return DryRunResponse(
        watchlist_name=request.watchlist_name,
        estimated_cost_usd=Decimal(str(estimate.total_usd)),
        estimate_basis=estimate.basis,
        estimate_confidence=estimate.confidence,
        symbol_count=estimate.symbol_count,
        breakdown=[
            {
                "symbol": line.symbol,
                "asset_class": line.asset_class,
                "dataset": line.dataset,
                "usd": line.usd,
            }
            for line in estimate.breakdown
        ],
    )


async def _compute_cost_estimate(request: OnboardRequest) -> Any:
    """Return the Databento cost estimate for the batch.

    Extracted as a seam so ``/onboard`` can reuse the same code path it
    runs on ``/onboard/dry-run`` for the cost-ceiling guardrail.
    """
    from msai.services.symbol_onboarding.cost_estimator import _DatabentoClientProto

    manifest = ParsedManifest(watchlist_name=request.watchlist_name, symbols=list(request.symbols))
    client = cast("_DatabentoClientProto", _get_databento_client())
    return await estimate_cost(manifest, client=client)


async def _get_arq_pool() -> Any:
    """Indirection seam for tests — returns an arq Redis pool."""
    return await get_redis_pool()


def _dedup_job_id(req: OnboardRequest, *, extra_parts: tuple[str, ...] = ()) -> str:
    """Build the deterministic ``_job_id`` used for arq dedup + the row's ``job_id_digest``.

    Canonical form orders symbols by ``(asset_class, symbol)`` so the same
    request submitted in any order produces the same digest. Includes
    ``cost_ceiling_usd`` so two otherwise-identical requests with
    different ceilings get distinct digests (otherwise the second
    request's ceiling would silently inherit the first's).
    """
    canonical = [
        f"{s.symbol}|{s.asset_class}|{s.start.isoformat()}|{s.end.isoformat()}"
        for s in sorted(req.symbols, key=lambda s: (s.asset_class, s.symbol))
    ]
    ceiling = str(req.cost_ceiling_usd) if req.cost_ceiling_usd is not None else "no_ceiling"
    digest = compute_blake2b_digest_key(
        "symbol_onboarding",
        req.watchlist_name,
        str(req.request_live_qualification),
        ceiling,
        *extra_parts,
        *canonical,
    )
    return f"symbol-onboarding:{digest:x}"


async def _enqueue_and_persist_run(
    db: AsyncSession,
    *,
    digest_hex: str,
    job_id: str,
    reserved_id: UUID,
    watchlist_name: str,
    symbol_states: dict[str, Any],
    request_live_qualification: bool,
    cost_ceiling_usd: Decimal | None,
    estimated_cost_usd: Decimal | None = None,
) -> OnboardResponse | JSONResponse:
    """Shared enqueue-first-then-commit helper.

    Step order:
    1. SELECT FOR UPDATE on digest. If row exists -> 200 OK + existing run_id (no enqueue).
    2. enqueue_job. If raises a known infra error -> 503 QUEUE_UNAVAILABLE (no row).
       Anything else propagates so programmer errors don't masquerade as 503.
       If returns None (race) -> sleep 100ms + re-SELECT; if row materializes
       -> 200 OK; else 409 DUPLICATE_IN_FLIGHT.
    3. Commit row. On commit failure -> rollback + best-effort abort_job (logged
       at WARN if it fails so orphan jobs are diagnosable) + re-raise.

    Dedup branches return ``JSONResponse(status_code=200)`` carrying the
    EXISTING row's ``status`` (not a hardcoded "pending" — a duplicate
    POST after the run completed must not claim it's still pending).
    Happy path returns plain ``OnboardResponse`` so FastAPI applies 202.
    """
    existing = (
        await db.execute(
            select(SymbolOnboardingRun)
            .where(SymbolOnboardingRun.job_id_digest == digest_hex)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        return JSONResponse(
            status_code=200,
            content=OnboardResponse(
                run_id=existing.id,
                watchlist_name=existing.watchlist_name,
                status=RunStatus(existing.status),
            ).model_dump(mode="json"),
        )

    try:
        pool = await _get_arq_pool()
    except _QUEUE_UNAVAILABLE_EXCEPTIONS as exc:
        log.warning("onboarding_pool_unavailable", error=repr(exc))
        return error_response(503, "QUEUE_UNAVAILABLE", "Job queue is unavailable.")

    try:
        job = await pool.enqueue_job(
            "run_symbol_onboarding",
            run_id=str(reserved_id),
            _job_id=job_id,
            _queue_name="msai:ingest",
        )
    except _QUEUE_UNAVAILABLE_EXCEPTIONS as exc:
        log.warning("onboarding_enqueue_failed", error=repr(exc))
        return error_response(503, "QUEUE_UNAVAILABLE", "Job queue rejected the submission.")

    if job is None:
        await asyncio.sleep(0.1)
        existing = (
            await db.execute(
                select(SymbolOnboardingRun).where(SymbolOnboardingRun.job_id_digest == digest_hex)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return JSONResponse(
                status_code=200,
                content=OnboardResponse(
                    run_id=existing.id,
                    watchlist_name=existing.watchlist_name,
                    status=RunStatus(existing.status),
                ).model_dump(mode="json"),
            )
        return error_response(
            409,
            "DUPLICATE_IN_FLIGHT",
            "Another onboarding request for the same watchlist is being submitted; retry in ~1s.",
        )

    run = SymbolOnboardingRun(
        id=reserved_id,
        watchlist_name=watchlist_name,
        status=SymbolOnboardingRunStatus.PENDING,
        symbol_states=symbol_states,
        request_live_qualification=request_live_qualification,
        cost_ceiling_usd=cost_ceiling_usd,
        estimated_cost_usd=estimated_cost_usd,
        job_id_digest=digest_hex,
    )
    db.add(run)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            await pool.abort_job(job.job_id)
        except Exception as abort_exc:  # noqa: BLE001 — log explicit so orphan jobs are diagnosable
            log.warning(
                "onboarding_abort_job_failed",
                job_id=job.job_id,
                run_id=str(reserved_id),
                error=repr(abort_exc),
            )
        raise
    await db.refresh(run)
    return OnboardResponse(
        run_id=run.id,
        watchlist_name=run.watchlist_name,
        status=RunStatus.PENDING,
    )


@router.post(
    "/onboard",
    response_model=OnboardResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def onboard(
    request: OnboardRequest,
    _user: Any = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> OnboardResponse | JSONResponse:
    """Start an async onboarding job.

    Idempotency: blake2b hash of (watchlist_name, symbols[],
    request_live_qualification) is the job id. Resubmitting an identical
    request returns 200 with the existing ``run_id`` (no second enqueue).

    **Errors:**
    - 200 OK — duplicate of an in-progress / committed run.
    - 202 Accepted — job started.
    - 409 Conflict — a concurrent submission claimed the slot first.
    - 503 Service Unavailable — Redis / arq queue rejected the submission.
    """
    estimated_cost: Decimal | None = None
    if request.cost_ceiling_usd is not None:
        estimate = await _compute_cost_estimate(request)
        estimated_cost = Decimal(str(estimate.total_usd))
        if estimated_cost > request.cost_ceiling_usd:
            return error_response(
                status_code=422,
                code="COST_CEILING_EXCEEDED",
                message=(
                    f"Estimated cost ${estimated_cost:.2f} exceeds "
                    f"ceiling ${request.cost_ceiling_usd:.2f}."
                ),
            )

    job_id = _dedup_job_id(request)
    digest_hex = job_id.removeprefix("symbol-onboarding:")
    symbol_states: dict[str, Any] = {
        spec.symbol: {
            "symbol": spec.symbol,
            "asset_class": spec.asset_class,
            "start": spec.start.isoformat(),
            "end": spec.end.isoformat(),
            "status": "not_started",
            "step": "pending",
            "error": None,
        }
        for spec in request.symbols
    }
    return await _enqueue_and_persist_run(
        db,
        digest_hex=digest_hex,
        job_id=job_id,
        reserved_id=uuid4(),
        watchlist_name=request.watchlist_name,
        symbol_states=symbol_states,
        request_live_qualification=request.request_live_qualification,
        cost_ceiling_usd=request.cost_ceiling_usd,
        estimated_cost_usd=estimated_cost,
    )


@router.get("/onboard/{run_id}/status", response_model=StatusResponse)
async def onboard_status(
    run_id: UUID = Path(...),  # noqa: B008
    _user: Any = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> StatusResponse | JSONResponse:
    """Poll the status of a running onboarding job.

    Returns the run's current state: overall status + per-symbol progress
    + aggregate counts (total, succeeded, failed, in_progress, not_started).

    **Errors:**
    - 404 Not Found — run_id does not exist.
    - 401 Unauthorized — JWT missing or invalid.
    """
    row = (
        await db.execute(select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id))
    ).scalar_one_or_none()
    if row is None:
        return error_response(status_code=404, code="NOT_FOUND", message=f"Run {run_id} not found")

    per_symbol = [
        SymbolStateRow(
            symbol=entry["symbol"],
            asset_class=entry["asset_class"],
            start=_date.fromisoformat(entry["start"]),
            end=_date.fromisoformat(entry["end"]),
            status=entry.get("status", "not_started"),
            step=entry.get("step", "pending"),
            error=entry.get("error"),
            next_action=_suggest_next_action(entry),
        )
        for entry in row.symbol_states.values()
    ]
    per_symbol.sort(key=lambda s: (s.asset_class, s.symbol))

    return StatusResponse(
        run_id=row.id,
        watchlist_name=row.watchlist_name,
        status=RunStatus(row.status),
        progress=_summarize(per_symbol),
        per_symbol=per_symbol,
        estimated_cost_usd=row.estimated_cost_usd,
        actual_cost_usd=row.actual_cost_usd,
    )


@router.post(
    "/onboard/{run_id}/repair",
    response_model=OnboardResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def onboard_repair(
    run_id: UUID = Path(...),  # noqa: B008
    body: dict[str, list[str]] | None = None,
    _user: Any = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> OnboardResponse | JSONResponse:
    """Retry only the failed symbols from a prior run.

    Spawns a NEW ``SymbolOnboardingRun`` (the original is left intact for
    audit). The repair body may carry ``{"symbols": [...]}`` to scope the
    retry; absent that, all parent ``failed`` rows retry.

    **Errors:**
    - 404 Not Found — parent run_id does not exist.
    - 409 Conflict — parent is still ``in_progress``.
    - 422 Unprocessable Entity — nothing to repair, or unknown symbol scope.
    """
    parent = (
        await db.execute(select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id))
    ).scalar_one_or_none()
    if parent is None:
        return error_response(status_code=404, code="NOT_FOUND", message=f"Run {run_id} not found")
    if parent.status == SymbolOnboardingRunStatus.IN_PROGRESS:
        return error_response(
            status_code=409,
            code="PARENT_RUN_IN_PROGRESS",
            message="Cannot repair while parent run is still in progress.",
        )

    target_symbols = (body or {}).get("symbols") or [
        entry["symbol"]
        for entry in parent.symbol_states.values()
        if entry.get("status") == "failed"
    ]
    if not target_symbols:
        return error_response(
            status_code=422,
            code="NO_FAILED_SYMBOLS",
            message="Nothing to repair — parent has no failed symbols.",
        )

    child_states: dict[str, Any] = {}
    for sym in target_symbols:
        parent_entry = parent.symbol_states.get(sym)
        if parent_entry is None:
            return error_response(
                status_code=422,
                code="UNKNOWN_SYMBOL",
                message=f"Symbol {sym!r} is not part of parent run {run_id}.",
            )
        child_states[sym] = {
            "symbol": sym,
            "asset_class": parent_entry["asset_class"],
            "start": parent_entry["start"],
            "end": parent_entry["end"],
            "status": "not_started",
            "step": "pending",
            "error": None,
        }

    child_digest = compute_blake2b_digest_key(
        "symbol_onboarding",
        f"{parent.watchlist_name}-repair",
        f"repair:{parent.id}",
        *sorted(target_symbols),
    )
    child_digest_hex = f"{child_digest:x}"
    child_job_id = f"symbol-onboarding:{child_digest_hex}"

    return await _enqueue_and_persist_run(
        db,
        digest_hex=child_digest_hex,
        job_id=child_job_id,
        reserved_id=uuid4(),
        watchlist_name=f"{parent.watchlist_name}-repair",
        symbol_states=child_states,
        request_live_qualification=parent.request_live_qualification,
        cost_ceiling_usd=parent.cost_ceiling_usd,
    )


def _summarize(per_symbol: list[SymbolStateRow]) -> OnboardProgress:
    total = len(per_symbol)
    succeeded = sum(1 for s in per_symbol if s.status == SymbolStatus.SUCCEEDED.value)
    failed = sum(1 for s in per_symbol if s.status == SymbolStatus.FAILED.value)
    in_progress = sum(1 for s in per_symbol if s.status == SymbolStatus.IN_PROGRESS.value)
    not_started = total - succeeded - failed - in_progress
    return OnboardProgress(
        total=total,
        succeeded=succeeded,
        failed=failed,
        in_progress=in_progress,
        not_started=not_started,
    )


def _suggest_next_action(entry: dict[str, Any]) -> str | None:
    if entry.get("status") != "failed":
        return None
    error_dict = entry.get("error") or {}
    code = error_dict.get("code") if isinstance(error_dict, dict) else None
    if not isinstance(code, str):
        return None
    mapping = {
        "BOOTSTRAP_AMBIGUOUS": "Disambiguate with exact instrument id + re-onboard.",
        "BOOTSTRAP_UNAUTHORIZED": "Check Databento dataset entitlement.",
        "BOOTSTRAP_UNMAPPED_VENUE": "File issue — unknown Databento venue MIC.",
        "COVERAGE_INCOMPLETE": "Inspect ingest logs; retry via /repair.",
        "IB_TIMEOUT": "Retry with request_live_qualification=false then rerun IB later.",
        "IB_UNAVAILABLE": "Confirm IB Gateway container is running + entitled.",
        "IB_NOT_CONFIGURED": (
            "Live qualification is not enabled in this build; "
            "rerun with request_live_qualification=false."
        ),
        "INGEST_FAILED": "Retry via /repair after checking Databento quota.",
    }
    return mapping.get(code)


@router.get("/readiness", response_model=ReadinessResponse)
async def readiness(
    symbol: str = Query(..., min_length=1, max_length=20),  # noqa: B008
    asset_class: str = Query(..., min_length=1, max_length=32),  # noqa: B008
    start: _date | None = Query(default=None),  # noqa: B008
    end: _date | None = Query(default=None),  # noqa: B008
    _user: Any = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> ReadinessResponse | JSONResponse:
    """Window-scoped per-instrument readiness.

    Returns the three-state readiness aggregate for ``(symbol,
    asset_class)``: ``registered`` / ``backtest_data_available`` /
    ``live_qualified`` (pin #3 — see PRD US-006).

    Without ``start`` + ``end``, ``backtest_data_available`` is ``null``
    (no window to scope against) and a short ``coverage_summary`` hint
    is returned. With both dates, the Parquet catalog is scanned and
    ``coverage_status`` reports ``full`` | ``gapped`` | ``none`` plus
    any ``missing_ranges``.

    **Errors:**
    - 404 NOT_FOUND — no active alias rows for ``(symbol, asset_class)``.
    - 401 Unauthorized — JWT missing or invalid.
    """
    master = SecurityMaster(db=db)
    try:
        resolution = await master.find_active_aliases(
            symbol=symbol,
            asset_class=asset_class,
            as_of_date=_date.today(),
        )
    except AmbiguousSymbolError as exc:
        return error_response(
            status_code=422,
            code="AMBIGUOUS_INSTRUMENT",
            message=(
                f"Symbol {symbol!r} ambiguous across "
                f"{len(exc.asset_classes)} definitions; use exact instrument id."
            ),
        )
    if resolution.instrument_uid is None:
        return error_response(
            status_code=404,
            code="NOT_FOUND",
            message=f"Symbol {symbol!r} not registered for asset_class={asset_class!r}",
        )

    live_qualified = resolution.has_ib_alias
    provider = resolution.primary_provider
    ingest_asset = normalize_asset_class_for_ingest(asset_class)

    if start is None or end is None:
        return ReadinessResponse(
            instrument_uid=resolution.instrument_uid,
            registered=True,
            provider=provider,
            backtest_data_available=None,
            coverage_status=None,
            covered_range=None,
            missing_ranges=[],
            live_qualified=live_qualified,
            coverage_summary=resolution.coverage_summary_hint(),
        )

    report = await compute_coverage(
        asset_class=ingest_asset,
        symbol=symbol,
        start=start,
        end=end,
        data_root=_FsPath(settings.data_root),
    )
    return ReadinessResponse(
        instrument_uid=resolution.instrument_uid,
        registered=True,
        provider=provider,
        backtest_data_available=(report.status == "full"),
        coverage_status=report.status,
        covered_range=report.covered_range,
        missing_ranges=[
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in report.missing_ranges
        ],
        live_qualified=live_qualified,
        coverage_summary=None,
    )
