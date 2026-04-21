"""Backtests API router -- launch, monitor, and retrieve backtest results.

Manages the full lifecycle of backtest runs: creation, status polling,
results retrieval, and history browsing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.core.queue import enqueue_backtest, get_redis_pool
from msai.models.backtest import Backtest
from msai.models.strategy import Strategy
from msai.models.trade import Trade
from msai.schemas.backtest import (
    BacktestListItem,
    BacktestListResponse,
    BacktestResultsResponse,
    BacktestRunRequest,
    BacktestStatusResponse,
    ErrorEnvelope,
    Remediation,
)
from msai.services.backtests.failure_code import FailureCode
from msai.services.backtests.sanitize import sanitize_public_message
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.nautilus.security_master.service import (
    DatabentoDefinitionMissing,
    SecurityMaster,
)
from msai.services.strategy_registry import load_strategy_class

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/backtests", tags=["backtests"])


class StrategyConfigValidationError(Exception):
    """Raised by :func:`_prepare_and_validate_backtest_config` when the
    user-submitted config fails ``StrategyConfig.parse()``.

    Carries the structured 422 envelope that ``main.py``'s exception
    handler renders as a top-level ``{"error": {...}}`` JSON response
    per ``.claude/rules/api-design.md``. Raising this (instead of
    ``HTTPException(detail={...})``) avoids FastAPI's default
    ``{"detail": <x>}`` wrapper, which would produce the non-compliant
    ``{"detail": {"error": {...}}}`` shape observed during the
    2026-04-21 code review.
    """

    def __init__(self, *, field: str | None, message: str) -> None:
        self.field = field or "(unknown)"
        self.message = message
        super().__init__(f"{self.field}: {message}")

    def envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Strategy config failed validation",
                "details": [{"field": self.field, "message": self.message}],
            }
        }


def _prepare_and_validate_backtest_config(
    config: dict[str, Any],
    *,
    strategy_file_path: str,
    config_class_name: str | None,
    canonical_instruments: list[str],
) -> dict[str, Any]:
    """Prepare + server-authoritatively validate the backtest config.

    Why this lives at the API layer (Hawk council blocking objection #4 +
    Contrarian #2, 2026-04-20): CLI and API callers should hit the same
    validation surface as the UI. Worker-only validation means malformed
    payloads sit in the queue before failing, and the persisted
    ``Backtest.config`` diverges from what portfolio/live paths normalize
    to. Doing the merge + parse at the API means one source of truth.

    Prep: if the caller omitted ``instrument_id`` / ``bar_type``, inject
    the first canonical instrument — mirrors the worker's
    ``_prepare_strategy_config`` at
    ``backend/src/msai/workers/backtest_job.py:371``. Persisting the
    PREPARED dict (caller's code does this after the return) closes the
    divergence with portfolio_service's merge logic.

    Validation: load the strategy's ``*Config`` class by the name that
    **discovery persisted** (``Strategy.config_class``, not a suffix-swap
    guess — Codex/pr-toolkit code review 2026-04-21: suffix-swap silently
    skips validation for classes named ``FooStrategyConfig`` / ``FooParams``
    / any non-``"{Name}Config"`` shape). Dump the prepared dict to JSON,
    run ``StrategyConfig.parse()``. msgspec's ``ValidationError`` carries
    a ``$.<field>`` path that we extract for the 422 response so the
    frontend can highlight the bad field.

    Returns the prepared config dict on success; raises
    ``HTTPException(422)`` on ``msgspec.ValidationError`` with a
    structured ``detail`` payload keyed for ``frontend/src/lib/api.ts``.

    ``config_class_name = None`` is a legitimate state (strategy has no
    matching ``*Config`` class). Validation is silently skipped in that
    case — the worker's auto-discovery at backtest-runner time still
    catches malformed payloads.
    """
    import json

    import msgspec

    # --- Prep: inject canonical instruments to match worker behavior ---
    prepared = dict(config)
    if canonical_instruments:
        if "instrument_id" not in prepared:
            prepared["instrument_id"] = canonical_instruments[0]
        if "bar_type" not in prepared:
            prepared["bar_type"] = f"{canonical_instruments[0]}-1-MINUTE-LAST-EXTERNAL"

    # --- Locate config class ---
    if not config_class_name:
        log.info(
            "backtest_config_validation_skipped",
            reason="no_config_class",
            strategy_file=strategy_file_path,
        )
        return prepared

    strategy_path = Path(strategy_file_path)
    if not strategy_path.exists():
        log.warning(
            "backtest_config_validation_skipped",
            reason="strategy_file_missing",
            file_path=strategy_file_path,
        )
        return prepared

    try:
        config_cls = load_strategy_class(strategy_path, config_class_name)
    except ImportError:
        log.info(
            "backtest_config_validation_skipped",
            reason="config_class_not_importable",
            strategy_file=strategy_file_path,
            config_class=config_class_name,
        )
        return prepared

    # --- Parse ---
    try:
        config_cls.parse(json.dumps(prepared))
    except msgspec.ValidationError as exc:
        # msgspec error format: "<reason> - at `$.<field>`". Strip the
        # backtick wrapping, leading ``$.``, and any trailing
        # whitespace so the client receives a plain field name like
        # ``instrument_id`` that matches the keys in
        # ``schema.properties`` for inline-error rendering.
        raw = str(exc)
        field = None
        if " - at " in raw:
            _, _, path = raw.partition(" - at ")
            # Strip wrapping backticks + leading "$." → plain dotted path
            field = path.strip().strip("`").removeprefix("$.").strip()
        raise StrategyConfigValidationError(field=field, message=raw) from exc

    return prepared


@router.post(
    "/run",
    status_code=status.HTTP_201_CREATED,
    response_model=BacktestStatusResponse,
    # PRD contract: `error` field is ABSENT (not `null`) when the row is not
    # failed. exclude_none strips every None field — also strips null
    # started_at/completed_at until the worker populates them, which is the
    # correct presentation anyway.
    response_model_exclude_none=True,
)
async def run_backtest(
    body: BacktestRunRequest,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestStatusResponse:
    """Create a new backtest record and enqueue it for execution.

    The backtest is created with status ``pending`` and enqueued to the
    arq worker pool via Redis. The caller should poll ``GET /{job_id}/status``
    to track progress.

    """
    # Verify the strategy exists
    result = await db.execute(select(Strategy).where(Strategy.id == body.strategy_id))
    strategy: Strategy | None = result.scalar_one_or_none()

    if strategy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Strategy {body.strategy_id} not found",
        )

    # Compute strategy code hash from the source file so the backtest is
    # reproducibly pinned to the exact code version used at enqueue time.
    strategy_hash = "unknown"
    if strategy.file_path:
        strategy_file = Path(strategy.file_path)
        if strategy_file.exists():
            import hashlib

            strategy_hash = hashlib.sha256(strategy_file.read_bytes()).hexdigest()

    # The worker now pulls instrument / date fields directly from the
    # Backtest row, so ``config`` is forwarded to the Nautilus
    # StrategyConfig verbatim.  We still make a defensive copy so the
    # caller's dict is not mutated downstream.
    worker_config = dict(body.config)

    # Resolve every instrument the caller supplied through the DB-backed
    # registry BEFORE storing the backtest row. ``resolve_for_backtest``
    # is fail-loud on a warm-path miss (``DatabentoDefinitionMissing`` —
    # operator must run ``msai instruments refresh`` first) with one
    # exception: the ``<root>.Z.<N>`` continuous-futures synthesis path
    # calls Databento on cold-miss. Backtest resolution never needs an
    # IB round-trip, so ``qualifier=None``. ``databento_client`` is
    # ``None`` when the API key is unset — the resolver will raise a
    # ``ValueError`` with a clear message on the ``.Z.N`` cold-miss
    # path, which is the desired behaviour.
    databento_client = (
        DatabentoClient(settings.databento_api_key) if settings.databento_api_key else None
    )
    security_master = SecurityMaster(
        qualifier=None,
        db=db,
        databento_client=databento_client,
    )
    try:
        canonical_instruments = await security_master.resolve_for_backtest(
            body.instruments,
            start=body.start_date.isoformat(),
            end=body.end_date.isoformat(),
        )
    except DatabentoDefinitionMissing as exc:
        log.warning(
            "backtest_instrument_unresolved",
            symbols=body.instruments,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        log.warning(
            "backtest_instrument_value_error",
            symbols=body.instruments,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    # Server-authoritative config validation (Hawk council blocking
    # objection #4, 2026-04-20). Happens AFTER instrument resolve so
    # canonical IDs are available to inject into the config dict before
    # msgspec.parse — matches the worker's _prepare_strategy_config
    # behavior so persisted ``Backtest.config`` is the same shape as
    # what portfolio/live paths normalize to (Contrarian #2).
    worker_config = _prepare_and_validate_backtest_config(
        worker_config,
        strategy_file_path=strategy.file_path,
        config_class_name=strategy.config_class,
        canonical_instruments=canonical_instruments,
    )

    # Create the backtest record
    backtest = Backtest(
        strategy_id=body.strategy_id,
        strategy_code_hash=strategy_hash,
        config=worker_config,
        instruments=canonical_instruments,
        start_date=body.start_date,
        end_date=body.end_date,
        status="pending",
        progress=0,
    )
    db.add(backtest)
    # Flush so ``backtest.id`` is assigned before we enqueue it to arq.
    await db.flush()

    # Enqueue to arq BEFORE commit — if enqueue fails, rollback the row
    try:
        pool = await get_redis_pool()
        backtest.queue_name = "arq:queue"
        job_id = await enqueue_backtest(pool, str(backtest.id), strategy.file_path, worker_config)
        backtest.queue_job_id = job_id
    except Exception as exc:
        await db.rollback()
        log.error("backtest_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue backtest job — Redis may be unavailable",
        ) from exc

    await db.commit()
    await db.refresh(backtest)

    log.info("backtest_enqueued", backtest_id=str(backtest.id), strategy_id=str(body.strategy_id))

    return BacktestStatusResponse(
        id=backtest.id,
        status=backtest.status,
        progress=backtest.progress,
        started_at=backtest.started_at,
        completed_at=backtest.completed_at,
        error=_build_error_envelope(backtest),
        phase=None,
        progress_message=None,
    )


@router.get("/history", response_model=BacktestListResponse)
async def list_backtests(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestListResponse:
    """List past backtests with pagination."""
    # Count total
    count_result = await db.execute(select(func.count()).select_from(Backtest))
    total: int = count_result.scalar_one()

    # Fetch page
    offset = (page - 1) * page_size
    result = await db.execute(
        select(Backtest).order_by(Backtest.created_at.desc()).offset(offset).limit(page_size)
    )
    backtests = result.scalars().all()

    items = [
        BacktestListItem(
            id=bt.id,
            strategy_id=bt.strategy_id,
            status=bt.status,
            start_date=bt.start_date,
            end_date=bt.end_date,
            created_at=bt.created_at,
            # Only surface error fields on failed rows; sanitize-on-read
            # when error_public_message is NULL (pre-migration) but error_message set.
            error_code=bt.error_code if bt.status == "failed" else None,
            error_public_message=(
                (bt.error_public_message or sanitize_public_message(bt.error_message))
                if bt.status == "failed"
                else None
            ),
            phase=bt.phase,  # type: ignore[arg-type]
            progress_message=bt.progress_message,
        )
        for bt in backtests
    ]

    return BacktestListResponse(items=items, total=total)


@router.get(
    "/{job_id}/status",
    response_model=BacktestStatusResponse,
    # Same exclude_none contract as POST /run above.
    response_model_exclude_none=True,
)
async def get_backtest_status(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestStatusResponse:
    """Return the current status of a backtest run."""
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()

    if backtest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backtest {job_id} not found",
        )

    return BacktestStatusResponse(
        id=backtest.id,
        status=backtest.status,
        progress=backtest.progress,
        started_at=backtest.started_at,
        completed_at=backtest.completed_at,
        error=_build_error_envelope(backtest),
        phase=backtest.phase,  # type: ignore[arg-type]
        progress_message=backtest.progress_message,
    )


@router.get("/{job_id}/results", response_model=BacktestResultsResponse)
async def get_backtest_results(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestResultsResponse:
    """Return metrics, trade count, and individual trade rows for a backtest."""
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()

    if backtest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backtest {job_id} not found",
        )

    # Fetch every trade generated by this backtest so the UI can render
    # the trade log without a second round-trip.
    trade_rows_result = await db.execute(
        select(Trade).where(Trade.backtest_id == job_id).order_by(Trade.executed_at.asc())
    )
    trade_rows = trade_rows_result.scalars().all()

    trade_count = len(trade_rows)

    trades_payload: list[dict[str, Any]] = [
        {
            "id": str(trade.id),
            "instrument": trade.instrument,
            "side": trade.side,
            "quantity": float(trade.quantity),
            "price": float(trade.price),
            "pnl": float(trade.pnl) if trade.pnl is not None else 0.0,
            "commission": float(trade.commission) if trade.commission is not None else 0.0,
            "executed_at": trade.executed_at.isoformat(),
        }
        for trade in trade_rows
    ]

    return BacktestResultsResponse(
        id=backtest.id,
        metrics=backtest.metrics,
        trade_count=trade_count,
        trades=trades_payload,
    )


@router.get("/{job_id}/report")
async def get_backtest_report(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> FileResponse:
    """Return the QuantStats HTML report file for a completed backtest."""
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()

    if backtest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Backtest {job_id} not found",
        )

    if backtest.report_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No report available for backtest {job_id}",
        )

    # Path traversal protection: ensure resolved path is within expected directory
    report_file = Path(backtest.report_path).resolve()
    expected_dir = (Path(settings.data_root) / "reports").resolve()
    if not str(report_file).startswith(str(expected_dir)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid report path",
        )

    if not report_file.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report file not found on disk for backtest {job_id}",
        )

    return FileResponse(
        path=str(report_file),
        media_type="text/html",
        filename=f"backtest_{job_id}_report.html",
    )


def _build_error_envelope(row: Backtest) -> ErrorEnvelope | None:
    """Return the structured error envelope for a ``failed`` row, or ``None``.

    Non-failed rows (pending/running/completed) always return ``None``.
    Historical rows (pre-migration) with ``error_code == 'unknown'`` still
    surface with their stored ``error_message`` — US-006 null-safe read —
    but sanitized on the fly so raw paths / tokens don't leak.

    The migration deliberately does NOT backfill
    ``error_public_message`` from the raw ``error_message`` column,
    because that would leak unsanitized content. Instead, when the
    public column is NULL here AND the raw message is populated
    (pre-migration row or a classifier bug), we sanitize-on-read.
    """
    if row.status != "failed":
        return None

    code = FailureCode.parse_or_unknown(row.error_code)
    message = (
        row.error_public_message
        or sanitize_public_message(row.error_message)
        or f"Backtest failed (code={code.value}); see server logs for details"
    )

    remediation = None
    if row.error_remediation is not None:
        remediation = Remediation.model_validate(row.error_remediation)

    return ErrorEnvelope(
        code=code.value,
        message=message,
        suggested_action=row.error_suggested_action,
        remediation=remediation,
    )
