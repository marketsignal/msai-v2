"""Backtests API router -- launch, monitor, and retrieve backtest results.

Manages the full lifecycle of backtest runs: creation, status polling,
results retrieval, and history browsing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves path param types at runtime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from msai.api._common import error_response
from msai.core.auth import get_current_user, get_current_user_or_none
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
    BacktestReportTokenResponse,
    BacktestResultsResponse,
    BacktestRunRequest,
    BacktestStatusResponse,
    BacktestTradeItem,
    BacktestTradesResponse,
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
from msai.services.observability.trading_metrics import (
    msai_backtest_results_payload_bytes,
    msai_backtest_trades_page_count,
)
from msai.services.report_signer import (
    InvalidReportTokenError,
    sign_report_token,
    verify_report_token,
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
    ``{"detail": {"error": {...}}}`` shape.
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


@lru_cache(maxsize=1)
def _resolved_reports_dir(data_root: str) -> Path:
    """Cache ``{data_root}/reports`` resolution.

    ``settings.data_root`` is effectively static for the process lifetime
    (env-var driven), so we resolve once and reuse. Keyed on ``data_root``
    so test monkeypatches that swap it invalidate cleanly.
    """
    return (Path(data_root) / "reports").resolve()


def _report_is_deliverable(report_path: str | None) -> bool:
    """Return ``True`` iff ``/report`` would actually serve the file today.

    Shared eligibility gate for ``/results`` (``has_report`` flag),
    ``/report-token`` (mint refusal), and ``/report`` itself. Checks:
    (1) path is set, (2) resolved path stays under ``{data_root}/reports``,
    (3) file exists on disk. Keeping the three endpoints in sync prevents
    the UI from opening a "Full report" tab whose signed URL ``/report``
    then rejects.
    """
    if report_path is None:
        return False
    resolved = Path(report_path).resolve()
    expected_dir = _resolved_reports_dir(settings.data_root)
    if not resolved.is_relative_to(expected_dir):
        return False
    return resolved.is_file()


def _prepare_and_validate_backtest_config(
    config: dict[str, Any],
    *,
    strategy_file_path: str,
    config_class_name: str | None,
    canonical_instruments: list[str],
) -> dict[str, Any]:
    """Prepare + server-authoritatively validate the backtest config.

    Server-side validation so CLI / API / UI callers hit the same surface,
    malformed payloads fail fast (not in the worker queue), and
    persisted ``Backtest.config`` matches what portfolio/live paths normalize to.

    Prep: inject the first canonical instrument into ``instrument_id`` /
    ``bar_type`` if missing — mirrors the worker's ``_prepare_strategy_config``.

    Validation: load the strategy's ``*Config`` class via the name that
    discovery persisted (``Strategy.config_class``), run
    ``StrategyConfig.parse()`` on the prepared dict, and re-raise as
    :class:`StrategyConfigValidationError` with the ``$.<field>`` path
    extracted from msgspec so the frontend can highlight the bad field.

    Returns the prepared config dict on success. Validation is skipped when
    ``config_class_name`` is ``None`` (strategy has no matching ``*Config``
    class); the worker's auto-discovery still catches malformed payloads
    at backtest-runner time.
    """
    import json

    import msgspec

    # --- Prep: inject canonical instruments to match worker behavior ---
    prepared = dict(config)
    if canonical_instruments:
        canonical_id = canonical_instruments[0]
        # **Always overwrite ``instrument_id``** with the canonical form
        # from the resolver. The 2026-05-12 data-path-closure work made
        # the read-boundary resolver accept both Databento MIC
        # (``AAPL.XNAS``) and exchange-name (``AAPL.NASDAQ``) input.
        # Leaving the user's input form here would make the Nautilus
        # subprocess read the catalog at the wrong path → zero bars →
        # ``trade_count=0``.
        prepared["instrument_id"] = canonical_id

        # **Rewrite only the instrument PREFIX in ``bar_type``** (Codex
        # P1 catch, PR #61 round 4). The bar_type format is
        # ``<instrument_id>-<step>-<aggregation>-<price_type>-<source>``
        # (e.g. ``AAPL.NASDAQ-5-MINUTE-LAST-EXTERNAL``). Callers
        # legitimately pick non-default step/aggregation/price_type/
        # source values (5-minute bars, hourly, BID/ASK price types,
        # INTERNAL source for synthetic bars). Unconditionally
        # rewriting the whole string to
        # ``{canonical_id}-1-MINUTE-LAST-EXTERNAL`` silently erases
        # those choices. Surgical rewrite: replace just the instrument
        # prefix (everything before ``-<digit>``), preserving the rest.
        user_bar_type = prepared.get("bar_type")
        if isinstance(user_bar_type, str) and "-" in user_bar_type:
            # The ``<step>`` segment is always numeric — split on the
            # first ``-<digit>`` boundary to find the instrument prefix.
            import re

            m = re.match(r"^(.+?)-(\d.*)$", user_bar_type)
            if m is not None:
                _old_prefix, rest = m.group(1), m.group(2)
                prepared["bar_type"] = f"{canonical_id}-{rest}"
            else:
                # Unparseable — fall back to the canonical default.
                prepared["bar_type"] = f"{canonical_id}-1-MINUTE-LAST-EXTERNAL"
        else:
            prepared["bar_type"] = f"{canonical_id}-1-MINUTE-LAST-EXTERNAL"

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
        # msgspec format: "<reason> - at `$.<field>`". Strip backticks +
        # leading "$." so the client receives a plain key (e.g.
        # ``instrument_id``) matching ``schema.properties`` for inline rendering.
        raw = str(exc)
        field = None
        if " - at " in raw:
            _, _, path = raw.partition(" - at ")
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
    request: Request,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestStatusResponse:
    """Create a new backtest record and enqueue it for execution.

    The backtest is created with status ``pending`` and enqueued to the
    arq worker pool via Redis. The caller should poll ``GET /{job_id}/status``
    to track progress.

    """
    # Gate on the smoke flag (Codex P2 catches, PR #61 rounds 3+4).
    # ``BacktestRunRequest.smoke`` is for the deploy-time data-path
    # smoke (``scripts/deploy-smoke.sh``) which runs ON the prod VM and
    # connects to the API DIRECTLY at ``127.0.0.1:8000`` (bypasses Caddy).
    # Any other caller setting ``smoke=True`` could hide their backtest
    # from ``/backtests/history`` AND make it eligible for the rollback
    # cleanup ``DELETE``.
    #
    # Trust signal: **absence of ``X-Forwarded-For``**. Caddy is the
    # sole public ingress on the prod VM; it injects ``X-Forwarded-For``
    # on every proxied request. The smoke script's
    # ``curl http://127.0.0.1:8000`` bypasses Caddy → no ``XFF``. An
    # earlier version of this gate (round 3) also required
    # ``client.host in {127.0.0.1, ::1, localhost}``, but that broke
    # legit smoke runs: inside the backend container the host's
    # loopback request arrives via the Docker bridge gateway IP
    # (typically ``172.x.x.x``), NOT literally ``127.0.0.1``, so the
    # allowlist stripped the legit flag. The XFF check alone is robust:
    # there's no network path to reach the backend's published port
    # without either (a) being on the host (compose binds
    # ``127.0.0.1:8000``, NOT a public interface) or (b) coming
    # through Caddy (which always sets XFF). Silent strip, not 4xx —
    # legitimate workflows that happen to include ``smoke=true``
    # shouldn't break; they just don't get the smoke privilege.
    if body.smoke:
        has_forwarded = "x-forwarded-for" in {k.lower() for k in request.headers}
        if has_forwarded:
            log.warning(
                "backtest_smoke_flag_stripped_from_external_caller",
                user=claims.get("preferred_username"),
            )
            body = body.model_copy(update={"smoke": False})
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

    # Resolve instruments through the DB-backed registry before storing
    # the row. ``resolve_for_backtest`` is fail-loud on a warm-path miss
    # (``DatabentoDefinitionMissing`` — operator runs ``msai instruments
    # refresh``), except continuous-futures (``<root>.Z.<N>``) which fall
    # through to a Databento cold-fetch. ``qualifier=None`` because backtest
    # resolution never needs IB; ``databento_client=None`` (unset API key)
    # raises a ValueError on the continuous-futures path, which is desired.
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

    # Validation happens AFTER instrument resolve so canonical IDs are
    # injected before msgspec.parse — matches the worker's
    # _prepare_strategy_config behavior.
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
        # Tag deploy-time data-path smoke so it stays out of human history
        # and is eligible for rollback cleanup. ``BacktestRunRequest.smoke``
        # defaults to ``False`` — only the deploy pipeline sets it ``True``.
        smoke=body.smoke,
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
    include_smoke: bool = Query(
        default=False,
        description=(
            "Include deploy-time data-path smoke backtests in the results. "
            "Smoke rows are written by ``deploy-on-vm.sh`` Phase 12 to prove "
            "the data path works on the prod VM after each deploy; they are "
            "filtered out by default because operators don't want them in "
            "their human history. Set ``true`` for diagnostic / audit views."
        ),
    ),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestListResponse:
    """List past backtests with pagination.

    Deploy-time smoke backtests are filtered by default (``smoke=False``).
    Pass ``include_smoke=true`` to see them — useful when diagnosing a
    deploy failure or auditing the smoke history.
    """
    # Build the optionally-filtered query once and reuse for count + page.
    base_query = select(Backtest)
    count_query = select(func.count()).select_from(Backtest)
    if not include_smoke:
        base_query = base_query.where(Backtest.smoke.is_(False))
        count_query = count_query.where(Backtest.smoke.is_(False))

    count_result = await db.execute(count_query)
    total: int = count_result.scalar_one()

    offset = (page - 1) * page_size
    result = await db.execute(
        base_query.order_by(Backtest.created_at.desc()).offset(offset).limit(page_size)
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
) -> BacktestResultsResponse | JSONResponse:
    """Return aggregate metrics + canonical series payload + trade count.

    Trades are no longer inline — see ``GET /api/v1/backtests/{id}/trades``
    for paginated fills. ``has_report`` is derived server-side from
    ``Backtest.report_path is not None`` + a file-existence check; the raw
    path is never exposed in the response.
    """
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()

    if backtest is None:
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            f"Backtest {job_id} not found",
        )

    # Aggregate count only — trade rows are paginated via /trades.
    trade_count_result = await db.execute(
        select(func.count()).select_from(Trade).where(Trade.backtest_id == job_id)
    )
    trade_count = trade_count_result.scalar_one()

    has_report = _report_is_deliverable(backtest.report_path)

    response = BacktestResultsResponse(
        id=backtest.id,
        metrics=backtest.metrics,
        # DB stores the JSONB payload as ``dict[str, Any] | None``; Pydantic
        # coerces the dict into ``SeriesPayload`` at response-model build time.
        # The worker only writes dicts that already conform to the schema
        # (it builds them via ``build_series_payload()``), so the coercion
        # is lossless at runtime.
        series=backtest.series,  # type: ignore[arg-type]
        trade_count=trade_count,
        # DB stores series_status as VARCHAR(32); the schema types it as the
        # ``SeriesStatus`` Literal. Narrow at the boundary — runtime values
        # are guaranteed to be one of the enum strings by the migration's
        # CHECK constraint + the worker's atomic write.
        series_status=backtest.series_status,  # type: ignore[arg-type]
        has_report=has_report,
    )
    # Observe response-body size so SRE can compare against the worker-write
    # site in ``_materialize_series_payload`` and detect accidental payload
    # bloat. ``model_dump_json`` serializes once (avoids the dict-intermediate
    # walk of ``_json.dumps(model_dump(mode="json"))``).
    msai_backtest_results_payload_bytes.observe(len(response.model_dump_json().encode("utf-8")))
    return response


# ---------------------------------------------------------------------------
# GET /{id}/trades — paginated fill log
# ---------------------------------------------------------------------------
# Oversize ``page_size`` is clamped (not 422'd) to match other list endpoints.
# Secondary sort on ``Trade.id ASC`` gives deterministic pagination when
# multiple fills share the same ``executed_at``.

MAX_TRADE_PAGE_SIZE = 500
DEFAULT_TRADE_PAGE_SIZE = 100
# Page_size histogram buckets — cap cardinality to 3 labels rather than
# one time series per distinct page_size value.
_PAGE_SIZE_SMALL_BUCKET = 100
_PAGE_SIZE_MEDIUM_BUCKET = 250


@router.get("/{job_id}/trades", response_model=BacktestTradesResponse)
async def get_backtest_trades(
    job_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_TRADE_PAGE_SIZE, ge=1),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestTradesResponse | JSONResponse:
    """Return paginated individual fills for a backtest.

    Sorted by ``(executed_at, id) ASC`` so results are deterministic even
    when multiple fills share the same timestamp.

    ``page_size`` is clamped to ``MAX_TRADE_PAGE_SIZE`` (500) server-side;
    oversized requests are truncated rather than rejected so the endpoint
    is robust against imprecise clients (project convention — matches
    ResearchJobListResponse + GraduationCandidateListResponse).
    """
    effective_page_size = min(page_size, MAX_TRADE_PAGE_SIZE)
    if effective_page_size <= _PAGE_SIZE_SMALL_BUCKET:
        page_size_bucket = f"<={_PAGE_SIZE_SMALL_BUCKET}"
    elif effective_page_size <= _PAGE_SIZE_MEDIUM_BUCKET:
        page_size_bucket = f"<={_PAGE_SIZE_MEDIUM_BUCKET}"
    else:
        page_size_bucket = f"<={MAX_TRADE_PAGE_SIZE}"
    msai_backtest_trades_page_count.labels(page_size=page_size_bucket).inc()

    exists_result = await db.execute(select(Backtest.id).where(Backtest.id == job_id))
    if exists_result.scalar_one_or_none() is None:
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            f"Backtest {job_id} not found",
        )

    total_result = await db.execute(
        select(func.count()).select_from(Trade).where(Trade.backtest_id == job_id)
    )
    total = total_result.scalar_one()

    offset = (page - 1) * effective_page_size
    rows_result = await db.execute(
        select(Trade)
        .where(Trade.backtest_id == job_id)
        # Secondary sort on id breaks ties on equal executed_at — deterministic
        # pagination across pages.
        .order_by(Trade.executed_at.asc(), Trade.id.asc())
        .offset(offset)
        .limit(effective_page_size)
    )
    rows = list(rows_result.scalars().all())

    items = [
        BacktestTradeItem(
            id=r.id,
            instrument=r.instrument,
            # Worker writes ``OrderSide.name`` which emits only "BUY"/"SELL";
            # schema narrows to that Literal. Runtime-guaranteed by the writer.
            side=r.side,  # type: ignore[arg-type]
            quantity=float(r.quantity),
            price=float(r.price),
            pnl=float(r.pnl) if r.pnl is not None else 0.0,
            commission=float(r.commission) if r.commission is not None else 0.0,
            executed_at=r.executed_at,
        )
        for r in rows
    ]

    return BacktestTradesResponse(
        items=items,
        total=total,
        page=page,
        page_size=effective_page_size,
    )


@router.post(
    "/{job_id}/report-token",
    response_model=BacktestReportTokenResponse,
)
async def mint_backtest_report_token(
    job_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> BacktestReportTokenResponse | JSONResponse:
    """Mint a short-lived signed URL for the report iframe.

    The returned URL embeds an HMAC token scoped to
    ``(backtest_id, user_sub, expires_at)``. The frontend loads the URL
    verbatim as an iframe ``src`` — no Next.js proxy, no server-side
    API key in the browser container. Expires after
    ``settings.report_token_ttl_seconds`` (default 60s), so browser-history
    leakage is inert shortly after render.
    """
    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()
    if backtest is None:
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            f"Backtest {job_id} not found",
        )
    # Same eligibility gate the /report GET will enforce — reject the
    # mint here rather than handing out a signed URL the downstream GET
    # will refuse. Keeps the UI's "Full report" tab honest.
    if not _report_is_deliverable(backtest.report_path):
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "NO_REPORT",
            "Report not available",
        )

    user_sub = claims.get("sub", "unknown")
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.report_token_ttl_seconds)
    token = sign_report_token(
        backtest_id=job_id,
        user_sub=user_sub,
        expires_at=expires_at,
        secret=settings.report_signing_secret,
    )
    return BacktestReportTokenResponse(
        signed_url=f"/api/v1/backtests/{job_id}/report?token={token}",
        expires_at=expires_at,
    )


@router.get("/{job_id}/report", response_model=None)
async def get_backtest_report(
    job_id: UUID,
    token: str | None = None,
    claims: dict[str, Any] | None = Depends(get_current_user_or_none),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> FileResponse | JSONResponse:
    """Return the QuantStats HTML report file for a completed backtest.

    Accepts either:

    - A signed ``?token=<hmac>`` query param scoped to this backtest_id
      (minted via ``POST /{id}/report-token``), used for iframe auth
      without a Next.js proxy; OR
    - Bearer JWT / ``X-API-Key`` header auth — the traditional
      ``get_current_user`` contract.

    When both are absent, returns 401 ``UNAUTHENTICATED``. When ``?token=``
    is present but invalid / expired / minted for a different backtest,
    returns 401 ``INVALID_TOKEN``.
    """
    if token is not None:
        try:
            token_claims = verify_report_token(
                token,
                backtest_id=job_id,
                secret=settings.report_signing_secret,
            )
        except InvalidReportTokenError as exc:
            return error_response(
                status.HTTP_401_UNAUTHORIZED,
                "INVALID_TOKEN",
                str(exc),
            )
        # Cross-user replay guard: when a session is attached, its ``sub``
        # must match the token's ``user_sub``. Pure signed-URL iframe fetches
        # (no session) stand on the token alone — that's the capability model.
        if claims is not None and claims.get("sub") != token_claims.user_sub:
            # WARNING so the forensic trail survives even if the browser
            # silently absorbs the 403.
            log.warning(
                "backtest_report_token_sub_mismatch",
                backtest_id=str(job_id),
                session_sub=claims.get("sub"),
                token_user_sub=token_claims.user_sub,
            )
            return error_response(
                status.HTTP_403_FORBIDDEN,
                "TOKEN_SUB_MISMATCH",
                "Report token was not minted for this session",
            )
        log.info(
            "backtest_report_served",
            backtest_id=str(job_id),
            auth_via="token",
            token_user_sub=token_claims.user_sub,
        )
    elif claims is None:
        return error_response(
            status.HTTP_401_UNAUTHORIZED,
            "UNAUTHENTICATED",
            "Missing auth",
        )

    result = await db.execute(select(Backtest).where(Backtest.id == job_id))
    backtest: Backtest | None = result.scalar_one_or_none()

    if backtest is None:
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "NOT_FOUND",
            f"Backtest {job_id} not found",
        )

    if backtest.report_path is None:
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "NO_REPORT",
            f"No report available for backtest {job_id}",
        )

    # Containment check — ``Path.is_relative_to`` rejects prefix-collision
    # bypasses (``/app/data/reports_evil/x.html``) that ``str.startswith`` would
    # accept. Belt-and-suspenders in case user input ever influences report_path.
    report_file = Path(backtest.report_path).resolve()
    expected_dir = (Path(settings.data_root) / "reports").resolve()
    if not report_file.is_relative_to(expected_dir):
        return error_response(
            status.HTTP_403_FORBIDDEN,
            "FORBIDDEN",
            "Invalid report path",
        )

    if not report_file.is_file():
        return error_response(
            status.HTTP_404_NOT_FOUND,
            "REPORT_FILE_MISSING",
            f"Report file not found on disk for backtest {job_id}",
        )

    # ``content_disposition_type="inline"`` is critical: Starlette's default
    # ``attachment`` disposition would make browsers download the HTML rather
    # than render it in the "Full report" iframe. ``inline`` keeps the
    # filename hint for the "Download Report" button flow.
    return FileResponse(
        path=str(report_file),
        media_type="text/html",
        filename=f"backtest_{job_id}_report.html",
        content_disposition_type="inline",
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
