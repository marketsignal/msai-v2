"""Live trading API router -- deploy, monitor, and control live strategies.

Manages the full lifecycle of live/paper trading deployments: starting
strategies, stopping them, querying status, and emergency halt.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves the type at runtime for path params

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from msai.api.live_deps import get_command_bus, get_idempotency_store
from msai.core.audit import log_audit
from msai.core.auth import get_current_user, resolve_user_id
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.live_deployment import LiveDeployment
from msai.models.live_deployment_strategy import LiveDeploymentStrategy
from msai.models.live_node_process import LiveNodeProcess
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.live_portfolio_revision_strategy import LivePortfolioRevisionStrategy
from msai.models.strategy import Strategy
from msai.schemas.live import (
    LiveDeploymentInfo,
    LiveDeploymentStatusResponse,
    LiveKillAllResponse,
    LivePositionsResponse,
    LiveResumeResponse,
    LiveStartRequest,
    LiveStatusResponse,
    LiveStopRequest,
    LiveTradesResponse,
    PortfolioStartRequest,
)
from msai.services.live.deployment_identity import (
    derive_message_bus_stream,
    derive_portfolio_deployment_identity,
    derive_strategy_id_full,
    derive_trader_id,
    generate_deployment_slug,
)
from msai.services.live.failure_kind import FailureKind
from msai.services.live.flatness_service import (
    coalesce_or_publish_stop_with_flatness,
    poll_stop_report,
)
from msai.services.live.idempotency import (
    PERMANENT_FAILURE_KINDS,
    REGISTRY_FAILURE_KINDS,
    BodyMismatchReservation,
    CachedOutcome,
    EndpointOutcome,
    IdempotencyStore,
    InFlight,
    Reserved,
)
from msai.services.live_command_bus import (
    LIVE_COMMAND_GROUP,
    LIVE_COMMAND_STREAM,
    LiveCommandBus,  # noqa: TC001 — FastAPI Depends resolves at runtime
)
from msai.services.nautilus.trading_node import TradingNodeManager
from msai.services.risk_engine import RiskEngine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/live", tags=["live"])

# Module-level risk engine and trading node manager (singleton per process)
_risk_engine = RiskEngine()
_node_manager = TradingNodeManager(_risk_engine)

# Startup poll timing (Task 1.14). Defaults match plan v6 values.
START_POLL_TIMEOUT_S: float = 60.0
"""Max wall-clock seconds ``/api/v1/live/start`` waits for the
subprocess to reach ``ready`` or a terminal ``failed`` state before
returning ``EndpointOutcome.api_poll_timeout()``. Tests override via
the module-level global."""

START_POLL_INTERVAL_S: float = 0.25
"""Sleep between polls of ``live_node_processes.status`` inside
:func:`_poll_for_terminal`. Short enough that a fast ready path
doesn't waste seconds, long enough that the poll loop doesn't
hammer the DB."""

STOP_POLL_TIMEOUT_S: float = 60.0
"""Same meaning as :data:`START_POLL_TIMEOUT_S` for the ``/stop``
path — waits for the supervisor to flip the row to ``stopped`` or
``failed``."""

_HALT_KEY = "msai:risk:halt"
"""Redis key checked by ``/start`` (layer 2 of the three-layer
idempotency model — decision #16). Set by ``/kill-all``."""


# ---------------------------------------------------------------------------
# Shared helpers for the start/stop path
# ---------------------------------------------------------------------------


async def _halt_is_active(bus: LiveCommandBus) -> bool:
    """Read the halt flag from Redis via the command bus's client.

    Tests stub the bus's ``_redis`` attribute directly so this path
    is exercised without the global ``get_command_bus`` dependency.
    """
    return bool(await bus._redis.exists(_HALT_KEY))  # noqa: SLF001 — intentional


# Idle window for the supervisor-alive check. The supervisor's
# ``XREADGROUP`` uses ``BLOCK 5000ms`` (see
# ``live_command_bus.consume``) so a live consumer's ``idle`` never
# exceeds a few seconds. 15 s is 3× that — a consumer idle for
# longer than this is almost certainly a crashed/stopped supervisor
# whose group entry hasn't been cleaned up yet.
_SUPERVISOR_MAX_IDLE_MS = 15_000


async def _supervisor_is_alive(bus: LiveCommandBus) -> bool:
    """Return True when at least one ``live-supervisor`` consumer has
    been active within ``_SUPERVISOR_MAX_IDLE_MS``.

    Drill 2026-04-15 P0-A: the ``live-supervisor`` service is gated
    behind the ``broker`` compose profile and therefore absent from
    the default ``docker compose up`` stack. When the supervisor is
    down, ``/api/v1/live/start`` used to publish a command, poll the
    (never-created) ``live_node_processes`` row until its 60 s
    deadline, and return 504 — a silent hang with no actionable
    error. Checking the consumer group's liveness here lets the
    endpoint return 503 with a clear remediation message the moment
    an operator forgets to activate the profile or the supervisor
    has crashed.
    """
    try:
        consumers = await bus._redis.xinfo_consumers(  # noqa: SLF001
            LIVE_COMMAND_STREAM, LIVE_COMMAND_GROUP
        )
    except Exception:  # noqa: BLE001 — any Redis error means "can't tell, assume dead"
        return False
    if not consumers:
        return False
    max_idle = _SUPERVISOR_MAX_IDLE_MS
    return any(int(c.get("idle", max_idle + 1)) < max_idle for c in consumers)


async def _poll_for_terminal(
    db: AsyncSession,
    deployment_id: UUID,
    *,
    ready_statuses: frozenset[str],
    terminal_statuses: frozenset[str],
    timeout_s: float,
    interval_s: float,
) -> LiveNodeProcess | None:
    """Poll the latest ``live_node_processes`` row for this deployment
    until its ``status`` lands in ``ready_statuses`` or
    ``terminal_statuses``, or until the deadline passes.

    Returns the row on success; returns ``None`` on timeout so the
    caller can produce :meth:`EndpointOutcome.api_poll_timeout`.

    Why a module-level function: tests monkeypatch this name to
    inject deterministic row transitions without driving a real
    supervisor. The default implementation hits the DB.

    **Precondition** (API-design trap): the caller MUST have
    committed any pending writes BEFORE calling this helper. The
    loop calls ``db.rollback()`` each iteration so a fresh
    transaction picks up writes the supervisor committed from
    another session (PostgreSQL's read-committed snapshot otherwise
    shows the row in its pre-poll state forever). A caller with
    uncommitted state would lose it on the first rollback.
    """
    deadline = monotonic() + timeout_s
    match_statuses = ready_statuses | terminal_statuses
    while monotonic() < deadline:
        # Start a fresh transaction every poll so we see writes the
        # supervisor committed from another session. Without this,
        # the caller's session keeps a snapshot of the row and the
        # status update never becomes visible.
        await db.rollback()
        row = (
            await db.execute(
                select(LiveNodeProcess)
                .where(LiveNodeProcess.deployment_id == deployment_id)
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None and row.status in match_statuses:
            return row
        await asyncio.sleep(interval_s)
    return None


def _apply_outcome(outcome: EndpointOutcome) -> JSONResponse:
    """Translate an :class:`EndpointOutcome` into a FastAPI
    :class:`JSONResponse` with the right HTTP status. Using
    ``JSONResponse`` directly (instead of ``raise HTTPException``)
    means the endpoint signature is consistent for every code path
    — success, already-active, transient, permanent — and the
    body is whatever the factory built."""
    return JSONResponse(status_code=outcome.status_code, content=outcome.response)


async def _resolve_user_id(db: AsyncSession, claims: dict[str, Any]) -> UUID | None:
    """Delegate to the shared helper in ``core.auth``.

    Kept as a thin wrapper so existing callers don't need renaming.
    See :func:`msai.core.auth.resolve_user_id` for full docstring.
    """
    return await resolve_user_id(db, claims)


@router.post("/start", deprecated=True)
async def live_start(
    request: LiveStartRequest,
) -> JSONResponse:
    """Deprecated single-strategy deploy endpoint.

    Use ``POST /api/v1/live/start-portfolio`` instead, which deploys
    an entire frozen portfolio revision to a specific IB account.
    """
    raise HTTPException(
        status_code=410,
        detail={
            "error": {
                "code": "ENDPOINT_DEPRECATED",
                "message": (
                    "POST /api/v1/live/start is deprecated. "
                    "Use POST /api/v1/live/start-portfolio instead."
                ),
            }
        },
    )


@router.post("/start-portfolio")
async def live_start_portfolio(  # noqa: PLR0912, PLR0915 — multi-branch dispatch by design
    request: PortfolioStartRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
    idem: IdempotencyStore = Depends(get_idempotency_store),  # noqa: B008
) -> JSONResponse:
    """Deploy a frozen portfolio revision to paper or live trading.

    This is the portfolio-based counterpart to :func:`live_start`.
    Instead of deploying a single strategy, it deploys an entire
    portfolio revision (a set of strategies with weights, configs,
    and instruments) to a specific IB account.

    The three-layer idempotency model is identical to ``/start``:

    1. **HTTP Idempotency-Key** — atomic SETNX reservation in Redis.
    2. **Halt flag** — if ``msai:risk:halt`` is set, return 503.
    3. **Identity-based warm restart** — the ``identity_signature``
       is computed from ``(user_id, portfolio_revision_id, account_id,
       paper_trading)`` via :class:`PortfolioDeploymentIdentity`.
    """
    # ------------------------------------------------------------------
    # Real-money safety gate (Codex Contrarian's blocking objection #1,
    # 2026-05-13 graduation-gate council). The graduation gate at
    # ``portfolio_service._is_graduated`` checks ``strategy_id`` only —
    # but portfolio members carry arbitrary ``config`` + ``instruments``.
    # Until the snapshot-binding follow-up lands (verifying that the
    # frozen revision member matches the approved GraduationCandidate),
    # live (real-money) deployments are blocked at this boundary.
    #
    # MUST fire BEFORE the idempotency layer below — otherwise a
    # cached outcome could replay a paper_trading=false response from a
    # prior call. With this gate first, no cached outcome can ever be
    # recorded for live, so replay is naturally safe.
    # ------------------------------------------------------------------
    if not request.paper_trading:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "LIVE_DEPLOY_BLOCKED",
                    "message": (
                        "Live (paper_trading=false) deployments are temporarily "
                        "blocked pending the snapshot-binding follow-up: the "
                        "portfolio member's config + instruments must be verified "
                        "against the approved GraduationCandidate snapshot before "
                        "real-money execution can proceed. Tracked in "
                        "docs/plans/2026-05-13-graduation-gate-promoted-orphan.md."
                    ),
                }
            },
        )

    # ------------------------------------------------------------------
    # Layer 1: HTTP Idempotency-Key reservation
    # ------------------------------------------------------------------
    user_id = await _resolve_user_id(db, claims)
    body_for_hash: dict[str, Any] = {
        "portfolio_revision_id": str(request.portfolio_revision_id),
        "account_id": request.account_id,
        "paper_trading": request.paper_trading,
        "ib_login_key": request.ib_login_key,
    }
    body_hash = IdempotencyStore.body_hash(body_for_hash)

    reservation: Reserved | None = None
    if idempotency_key is not None:
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Idempotency-Key requires an authenticated user.",
            )
        result = await idem.reserve(user_id=user_id, key=idempotency_key, body_hash=body_hash)
        if isinstance(result, InFlight):
            return _apply_outcome(EndpointOutcome.in_flight())
        if isinstance(result, CachedOutcome):
            return _apply_outcome(result.outcome)
        if isinstance(result, BodyMismatchReservation):
            return _apply_outcome(EndpointOutcome.body_mismatch())
        reservation = result

    try:
        # -------------------------------------------------------------
        # Layer 2: Halt flag
        # -------------------------------------------------------------
        if await _halt_is_active(bus):
            outcome = EndpointOutcome.halt_active()
            if reservation is not None:
                await idem.release(reservation.redis_key)
            return _apply_outcome(outcome)

        # -------------------------------------------------------------
        # Supervisor liveness
        # -------------------------------------------------------------
        if not await _supervisor_is_alive(bus):
            log.error(
                "portfolio_start_rejected_no_supervisor",
                extra={
                    "stream": LIVE_COMMAND_STREAM,
                    "group": LIVE_COMMAND_GROUP,
                },
            )
            if reservation is not None:
                await idem.release(reservation.redis_key)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "live-supervisor is not running. Start it with "
                    "`docker compose -f docker-compose.dev.yml --profile broker "
                    "up -d live-supervisor ib-gateway` and retry."
                ),
            )

        # -------------------------------------------------------------
        # Load frozen revision + members (SELECT FOR UPDATE)
        # -------------------------------------------------------------
        revision: LivePortfolioRevision | None = (
            await db.execute(
                select(LivePortfolioRevision)
                .where(LivePortfolioRevision.id == request.portfolio_revision_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if revision is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Portfolio revision {request.portfolio_revision_id} not found",
            )
        if not revision.is_frozen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Portfolio revision {request.portfolio_revision_id} is not frozen. "
                    "Freeze it before deploying."
                ),
            )

        members: list[LivePortfolioRevisionStrategy] = list(
            (
                await db.execute(
                    select(LivePortfolioRevisionStrategy)
                    .where(LivePortfolioRevisionStrategy.revision_id == revision.id)
                    .order_by(LivePortfolioRevisionStrategy.order_index)
                )
            )
            .scalars()
            .all()
        )
        if not members:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Portfolio revision {request.portfolio_revision_id} has no strategies. "
                    "Add at least one strategy before deploying."
                ),
            )

        # -------------------------------------------------------------
        # Load Strategy models and compute code hashes
        # -------------------------------------------------------------
        strategy_ids = [m.strategy_id for m in members]
        strategies_by_id: dict[UUID, Strategy] = {}
        for strat_row in (
            (await db.execute(select(Strategy).where(Strategy.id.in_(strategy_ids))))
            .scalars()
            .all()
        ):
            strategies_by_id[strat_row.id] = strat_row

        missing = [sid for sid in strategy_ids if sid not in strategies_by_id]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategies not found: {[str(s) for s in missing]}",
            )

        # Pick the first member's strategy for the deployment row's strategy_id.
        first_strategy = strategies_by_id[members[0].strategy_id]

        # Aggregate instruments + config from all members in single pass
        instrument_set: set[str] = set()
        combined_config: dict[str, Any] = {}
        for m in members:
            instrument_set.update(m.instruments)
            strat = strategies_by_id[m.strategy_id]
            combined_config[f"{strat.strategy_class}_{m.order_index}"] = m.config
        all_instruments = sorted(instrument_set)

        # -------------------------------------------------------------
        # Layer 3: Identity-based warm-restart upsert
        # -------------------------------------------------------------
        identity = derive_portfolio_deployment_identity(
            user_id=user_id,
            portfolio_revision_id=request.portfolio_revision_id,
            account_id=request.account_id,
            paper_trading=request.paper_trading,
            ib_login_key=request.ib_login_key,
            user_sub=claims.get("sub"),
        )
        identity_signature = identity.signature()

        # --------------------------------------------------------------
        # UNIQUE(revision_id, account_id) pre-insert gate (Bug #1 fix).
        # Changing ib_login_key produces a new identity_signature, but
        # the row would still collide with the existing one on the
        # (revision_id, account_id) UNIQUE constraint. Reject explicitly
        # with 422 rather than surfacing IntegrityError to the caller —
        # operator must archive/stop the existing row first.
        # See docs/plans/2026-05-13-live-deploy-safety-trio.md §Bug #1.
        # --------------------------------------------------------------
        existing_collision = (
            await db.execute(
                select(LiveDeployment).where(
                    LiveDeployment.portfolio_revision_id == request.portfolio_revision_id,
                    LiveDeployment.account_id == request.account_id,
                    LiveDeployment.identity_signature != identity_signature,
                )
            )
        ).scalar_one_or_none()
        if existing_collision is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "LIVE_DEPLOY_CONFLICT",
                        "message": (
                            "An existing deployment for this portfolio revision + account "
                            "exists with a different identity (different ib_login_key, "
                            "paper_trading, or other identity-bearing field). "
                            "Archive/delete the existing row OR re-submit with the same identity."
                        ),
                        "details": {
                            "existing_deployment_id": str(existing_collision.id),
                            "existing_status": existing_collision.status,
                            "existing_ib_login_key": existing_collision.ib_login_key,
                            "existing_paper_trading": existing_collision.paper_trading,
                            "requested_ib_login_key": request.ib_login_key,
                            "requested_paper_trading": request.paper_trading,
                            "hint": (
                                "stop the existing deployment via POST /api/v1/live/stop, "
                                "then retry"
                            ),
                        },
                    }
                },
            )

        slug = generate_deployment_slug()
        now = datetime.now(UTC)
        deployment_table = LiveDeployment.__table__

        stmt = pg_insert(LiveDeployment).values(
            strategy_id=first_strategy.id,
            status="starting",
            paper_trading=request.paper_trading,
            last_started_at=now,
            last_stopped_at=None,
            started_by=user_id,
            deployment_slug=slug,
            identity_signature=identity_signature,
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full(first_strategy.strategy_class, slug),
            account_id=request.account_id,
            ib_login_key=request.ib_login_key,
            message_bus_stream=derive_message_bus_stream(slug),
            portfolio_revision_id=request.portfolio_revision_id,
        )
        _active_statuses = ("starting", "building", "ready", "running")
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=[deployment_table.c.identity_signature],
            set_={
                "status": case(
                    (
                        deployment_table.c.status.in_(_active_statuses),
                        deployment_table.c.status,
                    ),
                    else_="starting",
                ),
                "last_started_at": now,
            },
        ).returning(deployment_table.c.id, deployment_table.c.deployment_slug)
        upsert_row = (await db.execute(upsert_stmt)).one()
        deployment_id = upsert_row.id
        is_warm_restart = upsert_row.deployment_slug != slug
        await db.commit()

        deployment = await db.get(LiveDeployment, deployment_id)
        if deployment is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Deployment row vanished between upsert and reload",
            )

        # -------------------------------------------------------------
        # Populate LiveDeploymentStrategy rows (idempotent on restart)
        # -------------------------------------------------------------
        # DELETE existing rows for this deployment so restarts don't
        # accumulate stale entries. Then INSERT one row per revision
        # member with the derived strategy_id_full.
        await db.execute(
            delete(LiveDeploymentStrategy).where(
                LiveDeploymentStrategy.deployment_id == deployment_id
            )
        )
        for member in members:
            strat = strategies_by_id[member.strategy_id]
            strategy_id_full = derive_strategy_id_full(
                strat.strategy_class,
                deployment.deployment_slug,
                member.order_index,
            )
            db.add(
                LiveDeploymentStrategy(
                    deployment_id=deployment_id,
                    revision_strategy_id=member.id,
                    strategy_id_full=strategy_id_full,
                )
            )
        await db.commit()

        # -------------------------------------------------------------
        # Active-process de-duplication
        # -------------------------------------------------------------
        active_process = (
            await db.execute(
                select(LiveNodeProcess)
                .where(
                    LiveNodeProcess.deployment_id == deployment.id,
                    LiveNodeProcess.status.in_(("starting", "building", "ready", "running")),
                )
                .order_by(LiveNodeProcess.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if active_process is not None:
            if active_process.status in ("ready", "running") and deployment.status != "running":
                deployment.status = "running"
                await db.commit()
            outcome = EndpointOutcome.already_active(
                {
                    "id": str(deployment.id),
                    "deployment_slug": deployment.deployment_slug,
                    "status": active_process.status,
                    "paper_trading": deployment.paper_trading,
                    "warm_restart": is_warm_restart,
                }
            )
            if reservation is not None:
                await idem.commit(reservation.redis_key, body_hash, outcome)
            return _apply_outcome(outcome)

        # -------------------------------------------------------------
        # Publish START command on the command bus
        # -------------------------------------------------------------
        await bus.publish_start(
            deployment_id=deployment.id,
            payload={
                "deployment_slug": deployment.deployment_slug,
                "strategy_id": str(first_strategy.id),
                "strategy_path": first_strategy.file_path,
                "config": combined_config,
                "instruments": all_instruments,
            },
            idempotency_key=idempotency_key,
        )

        # -------------------------------------------------------------
        # Graduation linking — link all member strategies
        # -------------------------------------------------------------
        try:
            from msai.models.graduation_candidate import GraduationCandidate

            for member in members:
                candidate = (
                    await db.execute(
                        select(GraduationCandidate).where(
                            GraduationCandidate.strategy_id == member.strategy_id,
                            GraduationCandidate.stage == "live_running",
                            GraduationCandidate.deployment_id.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if candidate is not None:
                    candidate.deployment_id = deployment.id
                    log.info(
                        "graduation_candidate_linked",
                        extra={
                            "candidate_id": str(candidate.id),
                            "deployment_id": str(deployment.id),
                            "strategy_id": str(member.strategy_id),
                        },
                    )
            await db.commit()
        except Exception:  # noqa: BLE001
            log.warning("graduation_candidate_link_failed", exc_info=True)

        # -------------------------------------------------------------
        # Register message_bus_stream with projection consumer
        # -------------------------------------------------------------
        try:
            from msai.main import get_stream_registry

            get_stream_registry().register(
                deployment_id=deployment.id,
                deployment_slug=deployment.deployment_slug,
                stream_name=deployment.message_bus_stream,
            )
        except Exception:  # noqa: BLE001
            log.debug("stream_registry_register_failed")

        # -------------------------------------------------------------
        # Poll live_node_processes for ready / failed / running
        # -------------------------------------------------------------
        row = await _poll_for_terminal(
            db,
            deployment.id,
            ready_statuses=frozenset({"ready", "running"}),
            terminal_statuses=frozenset({"failed", "stopped"}),
            timeout_s=START_POLL_TIMEOUT_S,
            interval_s=START_POLL_INTERVAL_S,
        )

        if row is None:
            outcome = EndpointOutcome.api_poll_timeout()
            if reservation is not None:
                await idem.release(reservation.redis_key)
            return _apply_outcome(outcome)

        if row.status in {"ready", "running"}:
            deployment.status = "running"
            deployment.last_stopped_at = None
            await db.commit()
            await log_audit(
                db,
                user_id=user_id,
                action="portfolio_start",
                resource_type="live_deployment",
                resource_id=deployment.id,
                details={
                    "portfolio_revision_id": str(request.portfolio_revision_id),
                    "account_id": request.account_id,
                    "member_count": len(members),
                    "instruments": all_instruments,
                    "paper": request.paper_trading,
                    "warm_restart": is_warm_restart,
                },
            )
            outcome = EndpointOutcome.ready(
                {
                    "id": str(deployment.id),
                    "deployment_slug": deployment.deployment_slug,
                    "status": row.status,
                    "paper_trading": deployment.paper_trading,
                    "warm_restart": is_warm_restart,
                }
            )
            if reservation is not None:
                await idem.commit(reservation.redis_key, body_hash, outcome)
            return _apply_outcome(outcome)

        # Terminal failure branch
        kind = FailureKind.parse_or_unknown(row.failure_kind)
        if kind is FailureKind.HALT_ACTIVE:
            outcome = EndpointOutcome.halt_active()
            if reservation is not None:
                await idem.release(reservation.redis_key)
            return _apply_outcome(outcome)

        if kind is FailureKind.SPAWN_FAILED_TRANSIENT:
            outcome = EndpointOutcome.spawn_failed_transient(
                row.error_message or "transient supervisor failure"
            )
            if reservation is not None:
                await idem.release(reservation.redis_key)
            return _apply_outcome(outcome)

        if kind not in PERMANENT_FAILURE_KINDS:
            kind = FailureKind.UNKNOWN
        if kind in REGISTRY_FAILURE_KINDS:
            outcome = EndpointOutcome.registry_permanent_failure(kind, row.error_message or "{}")
        else:
            outcome = EndpointOutcome.permanent_failure(
                kind, row.error_message or "unknown failure"
            )
        if reservation is not None:
            if outcome.cacheable:
                await idem.commit(reservation.redis_key, body_hash, outcome)
            else:
                await idem.release(reservation.redis_key)
        return _apply_outcome(outcome)

    except Exception:
        if reservation is not None:
            await idem.release(reservation.redis_key)
        raise


@router.post("/stop")
async def live_stop(
    request: LiveStopRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
) -> JSONResponse:
    """Stop a running deployment (Task 1.14).

    Publishes a STOP command to the live supervisor via
    :class:`LiveCommandBus` and polls ``live_node_processes`` until
    the latest row lands in ``stopped`` or ``failed``, with a 60 s
    wall-clock timeout. Idempotent: if no active ``live_node_processes``
    row exists, returns 200 with ``status=stopped`` immediately
    (already stopped).
    """
    result = await db.execute(
        select(LiveDeployment).where(LiveDeployment.id == request.deployment_id)
    )
    deployment: LiveDeployment | None = result.scalar_one_or_none()

    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deployment {request.deployment_id} not found",
        )

    # Idempotent short-circuit: no active process row → already stopped.
    active_process = (
        await db.execute(
            select(LiveNodeProcess)
            .where(
                LiveNodeProcess.deployment_id == deployment.id,
                LiveNodeProcess.status.in_(
                    ("starting", "building", "ready", "running", "stopping")
                ),
            )
            .order_by(LiveNodeProcess.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_process is None:
        return _apply_outcome(
            EndpointOutcome.stopped({"id": str(deployment.id), "status": "stopped"})
        )

    # Gather the deployment's member strategy_id_fulls so the child can
    # filter cache.positions_open() by member ownership (Bug #2, plan §3).
    member_rows = (
        await db.execute(
            select(LiveDeploymentStrategy.strategy_id_full).where(
                LiveDeploymentStrategy.deployment_id == deployment.id
            )
        )
    ).all()
    member_strategy_id_fulls = [row.strategy_id_full for row in member_rows]

    # Publish STOP_AND_REPORT_FLATNESS via SET-NX coalescing — concurrent
    # /stop callers converge on the originator's nonce (Codex iter-6 P2 #1).
    stop_nonce, _is_originator = await coalesce_or_publish_stop_with_flatness(
        redis=bus._redis,  # noqa: SLF001 — intentional bus.redis reuse
        bus=bus,
        deployment_id=deployment.id,
        member_strategy_id_fulls=member_strategy_id_fulls,
        reason="user",
        idempotency_key=idempotency_key,
    )

    # Poll for the child's STOP_REPORT (30 s deadline). Does NOT DEL —
    # 120 s TTL handles cleanup; coalesced readers share the key.
    report = await poll_stop_report(
        redis=bus._redis,  # noqa: SLF001
        stop_nonce=stop_nonce,
        deadline_s=30.0,
    )

    # Also poll the LiveNodeProcess row for terminal status — gives us
    # ``process_status`` for the response + lets us write
    # ``LiveDeployment.status='stopped'`` only once the supervisor has
    # confirmed the process exited.
    row = await _poll_for_terminal(
        db,
        deployment.id,
        ready_statuses=frozenset(),
        terminal_statuses=frozenset({"stopped", "failed"}),
        timeout_s=STOP_POLL_TIMEOUT_S,
        interval_s=START_POLL_INTERVAL_S,
    )

    # PR #65 Codex P1: only report ``status: stopped`` when the
    # supervisor confirms a terminal LiveNodeProcess row. A standalone
    # flatness report is insufficient — the child could write the
    # stop_report and then hang before dispose/exit, leaving the
    # subprocess alive while the API claims success. Treat
    # report-without-terminal-row as the timeout path so the operator
    # knows the supervisor side never closed out.
    if row is None:
        return _apply_outcome(EndpointOutcome.api_poll_timeout())

    deployment.status = "stopped"
    deployment.last_stopped_at = datetime.now(UTC)
    await db.commit()

    # PR #65 Codex P2: clear `inflight_stop:{deployment_id}` once the
    # supervisor has confirmed termination. Without this, a deployment
    # warm-restarted within the 60s TTL would have its next /stop call
    # coalesce onto THIS run's nonce — polling a stop_report from the
    # old process while the new one keeps running. Best-effort: if
    # Redis DEL fails, the 60s TTL is the fallback.
    with contextlib.suppress(Exception):
        await bus._redis.delete(f"inflight_stop:{deployment.id}")  # noqa: SLF001

    await log_audit(
        db,
        user_id=deployment.started_by,
        action="live_stop",
        resource_type="live_deployment",
        resource_id=deployment.id,
        details={
            "stop_nonce": stop_nonce,
            "broker_flat": report["broker_flat"] if report else None,
        },
    )

    log.info(
        "live_deployment_stopped",
        deployment_id=str(deployment.id),
        process_status=row.status,
        broker_flat=report["broker_flat"] if report else None,
        stop_nonce=stop_nonce,
    )

    return _apply_outcome(
        EndpointOutcome.stopped(
            {
                "id": str(deployment.id),
                "status": "stopped",
                "process_status": row.status,
                "stop_nonce": stop_nonce,
                "broker_flat": report["broker_flat"] if report else None,
                "remaining_positions": (report["remaining_positions"] if report else []),
            }
        )
    )


@router.post("/kill-all", response_model=None)
async def live_kill_all(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
) -> LiveKillAllResponse | JSONResponse:
    """Emergency stop ALL running strategies (Phase 3 task 3.9).

    The kill switch is **four layers** of defense:

    Layer 1 — **Persistent halt flag** (this endpoint). Sets
    ``msai:risk:halt`` in Redis with a 24h TTL. ``/start``
    reads this flag at the very top and returns 503 — blocks
    any NEW deployments from being launched at the API.

    Layer 2 — **Supervisor-side halt re-check** (Task 1.7
    ProcessManager.spawn). The supervisor re-checks the halt
    flag AFTER reserving the DB slot but BEFORE
    ``process.start()``. Catches commands queued in
    ``msai:live:commands`` before the kill-all and commands
    later reclaimed from the PEL via ``XAUTOCLAIM``. This is
    the v5 fix for Codex v4 P0.

    Layer 3 — **Push-based stop** (this endpoint). For every
    ``live_node_processes`` row with status in
    ``starting/building/ready/running``, publishes a stop
    command via :class:`LiveCommandBus`. The supervisor then
    SIGTERMs the subprocess and Nautilus's ``manage_stop=True``
    flatten loop closes positions automatically. Latency from
    ``/kill-all`` to flatten is < 5 seconds in normal
    operation.

    Layer 4 — **In-strategy halt-flag check**
    (RiskAwareStrategy mixin from Task 3.7). Refuses any new
    orders the strategy might emit between SIGTERM and the
    subprocess actually exiting. Defense in depth.

    The endpoint sets the halt flag FIRST (Layer 1) so that
    any concurrent ``/start`` request landing during the
    publish loop is also blocked.
    """
    from msai.services.observability.trading_metrics import KILL_SWITCH_ACTIVATED

    KILL_SWITCH_ACTIVATED.inc()

    halt_set_at = datetime.now(UTC)
    user_id = await _resolve_user_id(db, claims)

    # Layer 1: persistent halt flag with 24h TTL. The TTL
    # exists so a forgotten halt doesn't permanently brick
    # the platform after a restart — operators must
    # explicitly POST /resume to clear it before the TTL
    # expires.
    await bus._redis.set(_HALT_KEY, "true", ex=86400)  # noqa: SLF001 — intentional bus reuse
    await bus._redis.set(  # noqa: SLF001
        f"{_HALT_KEY}:set_by",
        str(user_id) if user_id else "unknown",
        ex=86400,
    )
    await bus._redis.set(  # noqa: SLF001
        f"{_HALT_KEY}:set_at",
        halt_set_at.isoformat(),
        ex=86400,
    )

    # Layer 3: query active live_node_processes rows and
    # publish a stop command for each. Use the explicit
    # status set so a row in a terminal state ('stopped',
    # 'failed') doesn't get a useless stop command.
    active_statuses = ("starting", "building", "ready", "running")
    rows = (
        (
            await db.execute(
                select(LiveNodeProcess).where(LiveNodeProcess.status.in_(active_statuses))
            )
        )
        .scalars()
        .all()
    )

    stopped = 0
    failed: list[str] = []
    flatness_nonces: dict[str, str] = {}  # deployment_id -> stop_nonce
    for row in rows:
        try:
            # Gather member strategy_id_fulls so the child reports
            # deployment-scoped flatness (Bug #2).
            member_rows = (
                await db.execute(
                    select(LiveDeploymentStrategy.strategy_id_full).where(
                        LiveDeploymentStrategy.deployment_id == row.deployment_id
                    )
                )
            ).all()
            members = [r.strategy_id_full for r in member_rows]

            nonce, _ = await coalesce_or_publish_stop_with_flatness(
                redis=bus._redis,  # noqa: SLF001
                bus=bus,
                deployment_id=row.deployment_id,
                member_strategy_id_fulls=members,
                reason="kill_switch",
            )
            flatness_nonces[str(row.deployment_id)] = nonce
            stopped += 1
        except Exception:  # noqa: BLE001
            failed.append(str(row.deployment_id))
            log.exception(
                "kill_switch_publish_stop_failed",
                deployment_id=str(row.deployment_id),
            )

    # Parallel-poll all stop_report keys with a single 15 s deadline
    # — slower than /stop's 30 s because kill-all is a panic surface
    # and the operator already knows positions need manual verification.
    flatness_results: dict[str, dict[str, Any] | None] = {}
    if flatness_nonces:

        async def _poll_one(dep_id: str, nce: str) -> tuple[str, dict[str, Any] | None]:
            return dep_id, await poll_stop_report(
                redis=bus._redis,  # noqa: SLF001
                stop_nonce=nce,
                deadline_s=15.0,
            )

        results = await asyncio.gather(*(_poll_one(d, n) for d, n in flatness_nonces.items()))
        flatness_results = dict(results)

    def _summarize(dep_id: str) -> dict[str, Any]:
        report = flatness_results.get(dep_id)
        return {
            "deployment_id": dep_id,
            "stop_nonce": flatness_nonces[dep_id],
            "broker_flat": report["broker_flat"] if report else None,
            "remaining_positions": report["remaining_positions"] if report else [],
        }

    flatness_summary = [_summarize(dep_id) for dep_id in flatness_nonces]
    any_non_flat = any(
        f["broker_flat"] is False or f["broker_flat"] is None for f in flatness_summary
    )

    await log_audit(
        db,
        user_id=user_id,
        action="live_kill_all",
        resource_type="live_deployment",
        details={
            "stopped_count": stopped,
            "failed_publish_count": len(failed),
            "failed_deployment_ids": failed,
            "halt_flag_set": True,
            "any_non_flat": any_non_flat,
            "flatness_reports": flatness_summary,
        },
    )

    if failed:
        # Codex batch 9 P1: an emergency-stop endpoint must
        # NEVER report success when it failed to stop
        # something. Surface the failures to the operator
        # via the response body AND a critical log line.
        # The halt flag IS still set (Layer 1) so any new
        # /start will be blocked, but the existing
        # deployments need manual attention.
        log.critical(
            "kill_all_executed_with_failures",
            stopped=stopped,
            failed_count=len(failed),
            failed_deployment_ids=failed,
        )
        return JSONResponse(
            status_code=207,  # Multi-Status — partial success
            content=LiveKillAllResponse(
                stopped=stopped,
                failed_publish=len(failed),
                risk_halted=True,
            ).model_dump(),
        )

    log.critical("kill_all_executed", stopped=stopped)
    return LiveKillAllResponse(stopped=stopped, failed_publish=0, risk_halted=True)


@router.post("/resume")
async def live_resume(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
) -> LiveResumeResponse:
    """Clear the persistent halt flag (Phase 3 task 3.9).

    Required before ``/start`` will accept new deployments
    again. There is intentionally NO auto-resume — the
    operator must explicitly unblock so a triggered kill
    switch doesn't silently re-allow trading after a
    cooldown. The 24h TTL on the halt flag is a safety net
    against the operator forgetting; the resume endpoint is
    the normal recovery path.

    Resume does NOT restart the previously-running
    deployments. Each deployment must be re-started
    individually via ``/start`` (which is the right policy:
    after a kill switch the operator should review the
    state before re-deploying).
    """
    user_id = await _resolve_user_id(db, claims)

    deleted = await bus._redis.delete(_HALT_KEY)  # noqa: SLF001
    await bus._redis.delete(f"{_HALT_KEY}:set_by")  # noqa: SLF001
    await bus._redis.delete(f"{_HALT_KEY}:set_at")  # noqa: SLF001

    await log_audit(
        db,
        user_id=user_id,
        action="live_resume",
        resource_type="live_deployment",
        details={"halt_flag_was_set": bool(deleted)},
    )

    log.warning("kill_switch_resumed", resumed_by=str(user_id))

    return LiveResumeResponse(resumed=True)


@router.get("/status")
async def live_status(
    active_only: bool = False,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LiveStatusResponse:
    """All deployments with their current status.

    Queries the database for recent deployments and combines that with
    the in-memory node manager status.

    Query params:
        active_only: if True, filter to deployments with status in
            {starting, running} and return ALL matches (no 50-row cap).
            Default False preserves the existing dashboard contract
            (50 most-recently-active deployments regardless of status).
            Added 2026-05-11 for the Slice 4 deploy.yml active-deployments
            gate — Codex PR #58 review caught that the 50-row default cap
            could push a long-running broker deployment off the response
            after 50+ subsequent stop events accumulate.
    """
    # Order by most recent activity, not by immutable ``created_at``:
    # since v9 a deployment row is a stable logical record that survives
    # restarts. "Most recent activity" is the max of the last-start,
    # last-stop, and created-at timestamps so a deployment stopped moments
    # ago ranks above one started long ago (Codex Task 1.1b iteration 5,
    # P2 fix). ``created_at`` is NOT NULL so the GREATEST always has a
    # floor; COALESCE each nullable column onto it so NULLs don't poison
    # the comparison.
    last_activity = func.greatest(
        func.coalesce(LiveDeployment.last_started_at, LiveDeployment.created_at),
        func.coalesce(LiveDeployment.last_stopped_at, LiveDeployment.created_at),
        LiveDeployment.created_at,
    )
    query = select(LiveDeployment).order_by(last_activity.desc())
    if active_only:
        # Active set must match the one main.py:158 uses on startup re-hydration —
        # PR #58 Codex round-4 P1: `building` and `ready` are written by paths
        # other than api/live.py (NautilusTrader subprocess + supervisor lifecycle
        # callbacks) and DO count as "live" for the deploy-gate's purposes. A
        # mismatch here causes the gate to fail open during the building/ready
        # window of a starting deployment.
        query = query.where(
            LiveDeployment.status.in_(["starting", "building", "ready", "running"])
        ).limit(1000)
    else:
        query = query.limit(50)
    result = await db.execute(query)
    deployments = result.scalars().all()

    items = [
        LiveDeploymentInfo(
            id=d.id,
            strategy_id=d.strategy_id,
            status=d.status,
            paper_trading=d.paper_trading,
            # ``instruments`` column was dropped in Task 11 — data now
            # lives on ``live_portfolio_revision_strategies``. Default
            # to empty list for backward-compatible API response.
            instruments=[],
            # Map the new most-recent-run timestamps onto the existing
            # response field names for backward compatibility. The
            # underlying columns were renamed in v9 task 1.1b but the
            # API contract is preserved.
            started_at=d.last_started_at,
            stopped_at=d.last_stopped_at,
        )
        for d in deployments
    ]

    return LiveStatusResponse(
        deployments=items,
        risk_halted=_risk_engine.is_halted,
        active_count=_node_manager.active_count,
    )


@router.get("/status/{deployment_id}", response_model=LiveDeploymentStatusResponse)
async def get_live_deployment_status(
    deployment_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008, ARG001
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LiveDeploymentStatusResponse:
    """Return the current status of a single live deployment (Task 1.13).

    Reads from the database — does NOT maintain or consult any
    in-memory state. The logical ``LiveDeployment`` row is joined with
    the most recent ``LiveNodeProcess`` row so the caller sees both
    the stable identity (slug, trader_id, config hash) AND the live
    per-run state (pid, host, heartbeat, terminal outcome).

    Returns 404 when ``deployment_id`` is unknown. Returns 200 with
    all process fields populated when a deployment has an active or
    recent ``live_node_processes`` row, and 200 with process fields
    as ``None`` when the deployment has never run.
    """
    deployment = (
        await db.execute(select(LiveDeployment).where(LiveDeployment.id == deployment_id))
    ).scalar_one_or_none()
    if deployment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"deployment_id {deployment_id} not found",
        )

    # Most recent ``live_node_processes`` row for this deployment. A row
    # may not exist (deployment never ran) — that's a 200 with
    # process fields = None.
    process = (
        await db.execute(
            select(LiveNodeProcess)
            .where(LiveNodeProcess.deployment_id == deployment_id)
            .order_by(LiveNodeProcess.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return LiveDeploymentStatusResponse(
        id=deployment.id,
        strategy_id=deployment.strategy_id,
        deployment_slug=deployment.deployment_slug,
        status=deployment.status,
        paper_trading=deployment.paper_trading,
        # ``instruments`` column dropped in Task 11 — default to empty list.
        instruments=[],
        last_started_at=deployment.last_started_at,
        last_stopped_at=deployment.last_stopped_at,
        process_id=process.id if process else None,
        pid=process.pid if process else None,
        host=process.host if process else None,
        process_status=process.status if process else None,
        last_heartbeat_at=process.last_heartbeat_at if process else None,
        exit_code=process.exit_code if process else None,
        error_message=process.error_message if process else None,
        failure_kind=process.failure_kind if process else None,
    )


@router.get("/positions")
async def live_positions(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LivePositionsResponse:
    """Open positions across all active deployments, read from ProjectionState.

    NOTE: This endpoint returns positions for ALL active deployments,
    not scoped to the authenticated user. This is by design — MSAI is
    a single-operator platform (not multi-tenant). If multi-operator
    support is added, filter by deployment.started_by == user_id.
    """
    from msai.api.live_deps import get_position_reader

    reader = get_position_reader()

    # Filter by the authoritative process-row state rather than
    # deployment.status. A deployment can lag at ``starting`` even
    # though its subprocess is fully ``ready``/``running`` — the UP
    # sync in ``already_active`` (Bug A, 2026-04-16) closes most of
    # the gap, but the /live/start poll-timeout race can still land
    # a permanent_failure response while a fresh subprocess comes
    # up in parallel. Going via the process row decouples the
    # /live/positions visibility from any deployment-row sync
    # latency. Includes BOTH running/ready so a newly-spawned
    # deployment's positions appear as soon as the subprocess
    # reports ready, and a truly-stopped deployment's positions
    # drop out immediately.
    active_process_statuses = ("ready", "running")
    latest_process_per_dep = (
        select(
            LiveNodeProcess.deployment_id,
            func.max(LiveNodeProcess.started_at).label("started_at"),
        )
        .group_by(LiveNodeProcess.deployment_id)
        .subquery()
    )
    active_rows = (
        (
            await db.execute(
                select(LiveDeployment)
                .join(
                    latest_process_per_dep,
                    latest_process_per_dep.c.deployment_id == LiveDeployment.id,
                )
                .join(
                    LiveNodeProcess,
                    (LiveNodeProcess.deployment_id == LiveDeployment.id)
                    & (LiveNodeProcess.started_at == latest_process_per_dep.c.started_at),
                )
                .where(LiveNodeProcess.status.in_(active_process_statuses))
            )
        )
        .scalars()
        .all()
    )

    all_positions: list[dict[str, Any]] = []
    for dep in active_rows:
        snapshots = await reader.get_open_positions(
            deployment_id=dep.id,
            trader_id=dep.trader_id,
            strategy_id_full=dep.strategy_id_full,
        )
        for snap in snapshots:
            all_positions.append(snap.model_dump(mode="json"))

    return LivePositionsResponse(positions=all_positions)


@router.get("/trades")
async def live_trades(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    deployment_id: UUID | None = Query(default=None),  # noqa: B008
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> LiveTradesResponse:
    """Recent live trade executions from order_attempt_audits.

    If ``deployment_id`` is provided, results are scoped to that
    deployment. Otherwise returns fills across all deployments.

    NOTE: Returns ALL live fills, not scoped to the authenticated user.
    Single-operator design — see /positions docstring for rationale.
    """
    from sqlalchemy.sql.elements import ColumnElement

    from msai.models.order_attempt_audit import OrderAttemptAudit

    base_filters: list[ColumnElement[bool]] = [
        OrderAttemptAudit.is_live.is_(True),
        OrderAttemptAudit.status.in_(("filled", "partially_filled")),
    ]
    if deployment_id is not None:
        base_filters.append(OrderAttemptAudit.deployment_id == deployment_id)

    count_q = select(func.count()).select_from(OrderAttemptAudit).where(*base_filters)
    total = (await db.execute(count_q)).scalar_one()

    rows_q = (
        select(OrderAttemptAudit)
        .where(*base_filters)
        .order_by(OrderAttemptAudit.ts_attempted.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(rows_q)).scalars().all()

    trades = [
        {
            "id": str(r.id),
            "deployment_id": str(r.deployment_id) if r.deployment_id else None,
            "instrument_id": r.instrument_id,
            "side": r.side,
            "quantity": str(r.quantity),
            "price": str(r.price) if r.price else None,
            "order_type": r.order_type,
            "status": r.status,
            "client_order_id": r.client_order_id,
            "timestamp": r.ts_attempted.isoformat(),
        }
        for r in rows
    ]

    return LiveTradesResponse(trades=trades, total=total)


@router.get("/audits/{deployment_id}")
async def live_audits(
    deployment_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    """Order attempt audits for a specific deployment.

    Used by the E2E harness to verify order submission.
    """
    from msai.models.order_attempt_audit import OrderAttemptAudit

    rows = (
        (
            await db.execute(
                select(OrderAttemptAudit)
                .where(OrderAttemptAudit.deployment_id == deployment_id)
                .order_by(OrderAttemptAudit.ts_attempted.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )

    return {
        "audits": [
            {
                "id": str(r.id),
                "client_order_id": r.client_order_id,
                "instrument_id": r.instrument_id,
                "side": r.side,
                "quantity": str(r.quantity),
                "status": r.status,
                "strategy_code_hash": r.strategy_code_hash,
                "timestamp": r.ts_attempted.isoformat(),
            }
            for r in rows
        ]
    }
