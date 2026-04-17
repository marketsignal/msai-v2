"""Live trading API router -- deploy, monitor, and control live strategies.

Manages the full lifecycle of live/paper trading deployments: starting
strategies, stopping them, querying status, and emergency halt.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID  # noqa: TC003 — FastAPI resolves the type at runtime for path params

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from msai.api.live_deps import get_command_bus, get_idempotency_store
from msai.core.audit import log_audit
from msai.core.auth import get_current_user, resolve_user_id
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.live_deployment import LiveDeployment
from msai.models.live_node_process import LiveNodeProcess
from msai.models.strategy import Strategy
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.live_portfolio_revision_strategy import LivePortfolioRevisionStrategy
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
    compute_config_hash,
    derive_deployment_identity,
    derive_message_bus_stream,
    derive_portfolio_deployment_identity,
    derive_strategy_id_full,
    derive_trader_id,
    generate_deployment_slug,
    normalize_request_config,
)
from msai.services.live.failure_kind import FailureKind
from msai.services.live.idempotency import (
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
from msai.services.strategy_registry import compute_file_hash

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


def _resolve_strategy_code_hash(strategy: Strategy) -> str:
    """Compute the SHA256 hash of the strategy source file.

    The hash is part of the stable-identity tuple (decision #7): editing
    a strategy file must produce a new ``identity_signature`` so the
    edited deployment starts cold with isolated state instead of
    silently warm-restarting on top of incompatible persisted state
    (Codex Task 1.1b P1 fix).

    ``Strategy.file_path`` is stored as a project-relative path (e.g.
    ``strategies/example/ema_cross.py``); resolve it against the
    configured ``strategies_root`` to get an absolute path on disk.
    """
    rel = Path(strategy.file_path)
    if rel.is_absolute():
        abs_path = rel
    elif rel.parts and rel.parts[0] == "strategies":
        # File path already includes the ``strategies/`` prefix;
        # drop it because strategies_root IS the strategies dir.
        abs_path = settings.strategies_root.joinpath(*rel.parts[1:])
    else:
        abs_path = settings.strategies_root / rel

    if not abs_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Strategy file not found on disk: {abs_path}. "
                "Re-register the strategy or restore the source file."
            ),
        )
    return compute_file_hash(abs_path)


@router.post("/start")
async def live_start(  # noqa: PLR0912, PLR0915 — multi-branch dispatch by design
    request: LiveStartRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
    idem: IdempotencyStore = Depends(get_idempotency_store),  # noqa: B008
) -> JSONResponse:
    """Deploy a strategy to paper or live trading (Task 1.14).

    Three idempotency layers (decision #13):

    1. **HTTP Idempotency-Key** — atomic SETNX reservation in Redis.
       Concurrent retries with the same key get HTTP 425 In-Flight;
       retries after completion get the cached response within the
       24 h TTL. Keys are user-scoped (Codex v4 P2) so an attacker
       cannot observe another user's cached response.

    2. **Halt flag** — if ``msai:risk:halt`` is set, return 503 with
       ``failure_kind=halt_active`` (non-cacheable so a ``/resume``
       followed by a retry can re-attempt).

    3. **Identity-based warm restart** — the ``identity_signature``
       is computed from ``(user_id, strategy_id, strategy_code_hash,
       config_hash, account_id, paper_trading, instruments)``. An
       existing row with the same signature is reused (warm restart);
       otherwise a fresh row is inserted (cold start). An existing
       row whose latest process is in an active status is a short-
       circuit ``already_active`` (200, cacheable).

    The endpoint publishes a START command to the live supervisor via
    :class:`LiveCommandBus` and polls ``live_node_processes`` for
    ``status in (ready, running, failed)`` with a 60 s wall-clock
    timeout. Terminal outcomes are classified via
    :meth:`FailureKind.parse_or_unknown` and converted into
    :class:`EndpointOutcome` values; the idempotency store's
    ``commit()`` / ``release()`` is driven by ``outcome.cacheable``.
    """
    # ------------------------------------------------------------------
    # Layer 1: HTTP Idempotency-Key reservation
    # ------------------------------------------------------------------
    user_id = await _resolve_user_id(db, claims)
    body_for_hash: dict[str, Any] = {
        "strategy_id": str(request.strategy_id),
        "config": request.config,
        "instruments": sorted(request.instruments),
        "paper_trading": request.paper_trading,
    }
    body_hash = IdempotencyStore.body_hash(body_for_hash)

    reservation: Reserved | None = None
    if idempotency_key is not None:
        if user_id is None:
            # Idempotency is user-scoped; an unresolved user has no
            # scope to anchor the reservation to. Fail fast rather
            # than silently falling back to a non-scoped key.
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
        # Only the Reserved branch owns the store (Codex v8 / v7 P0).
        # The caller MUST eventually call commit() or release().
        reservation = result

    # ``do_release_on_error`` wraps the rest of the handler; any
    # exception raised below triggers release() so the next retry
    # with the same key can re-attempt.
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
        # The live-supervisor container consumes START/STOP commands
        # from Redis Streams and spawns the Nautilus trading
        # subprocess. If no supervisor is actively consuming (the
        # ``broker`` compose profile wasn't activated, the container
        # crashed, etc.), publishing a command here would land it in
        # a stream with no reader and the 60 s poll below would time
        # out with a generic 504 — unhelpful during a deploy or
        # incident. Fail fast with a 503 + remediation instead.
        if not await _supervisor_is_alive(bus):
            log.error(
                "live_start_rejected_no_supervisor",
                extra={
                    "stream": LIVE_COMMAND_STREAM,
                    "group": LIVE_COMMAND_GROUP,
                    "note": (
                        "no live-supervisor consumer active — start the broker "
                        "profile or restart the live-supervisor container"
                    ),
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
        # Strategy lookup
        # -------------------------------------------------------------
        strategy: Strategy | None = (
            await db.execute(select(Strategy).where(Strategy.id == request.strategy_id))
        ).scalar_one_or_none()
        if strategy is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategy {request.strategy_id} not found",
            )

        # -------------------------------------------------------------
        # Layer 3: Identity-based warm-restart upsert
        # -------------------------------------------------------------
        strategy_code_hash = _resolve_strategy_code_hash(strategy)
        account_id = (settings.ib_account_id or "").strip() or "DU0000000"
        normalized_config = normalize_request_config(request.config, strategy.default_config)
        identity = derive_deployment_identity(
            user_id=user_id,
            strategy_id=request.strategy_id,
            strategy_code_hash=strategy_code_hash,
            config=normalized_config,
            account_id=account_id,
            paper_trading=request.paper_trading,
            instruments=request.instruments,
        )
        identity_signature = identity.signature()

        slug = generate_deployment_slug()
        now = datetime.now(UTC)
        deployment_table = LiveDeployment.__table__

        stmt = pg_insert(deployment_table).values(
            strategy_id=request.strategy_id,
            strategy_code_hash=strategy_code_hash,
            config=request.config,
            instruments=request.instruments,
            status="starting",
            paper_trading=request.paper_trading,
            last_started_at=now,
            last_stopped_at=None,
            started_by=user_id,
            deployment_slug=slug,
            identity_signature=identity_signature,
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full(strategy.strategy_class, slug),
            account_id=account_id,
            message_bus_stream=derive_message_bus_stream(slug),
            config_hash=identity.config_hash,
            instruments_signature=identity.instruments_signature,
        )
        # PR#1 Codex P1 regression: preserve the existing status when
        # the row is already in an active state. Before this fix, the
        # set_ clause unconditionally stomped ``status`` to
        # ``"starting"``, so a retry against a RUNNING deployment
        # would flip the deployment row from ``running`` back to
        # ``starting`` even though the downstream active_process
        # check returns already_active. That made
        # ``/api/v1/live/status`` report the wrong state for a live
        # deployment.
        #
        # The SQL CASE below keeps the existing status if it's in
        # {starting, building, ready, running} (the active set) and
        # otherwise resets it to ``starting`` for the cold-start /
        # warm-restart paths. Atomic, no race.
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
        # Active-process de-duplication — if a live_node_processes row
        # is already in an active state, return already_active (200,
        # cacheable) without publishing a new command.
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
            # UP-direction sync — mirror of PR #26 Fix A/B. When an
            # idempotent retry or warm restart hits an already-running
            # process, the deployment row can be stuck at 'starting'
            # (the upsert CASE above preserves any active status, so a
            # deployment whose original /live/start never completed the
            # poll-and-update step keeps its initial 'starting'). That
            # stale value makes /live/positions, /live/status, and the
            # UI filter out the deployment even though its process is
            # operational. Sync here so observers see operational truth.
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
                "strategy_id": str(request.strategy_id),
                "strategy_path": strategy.file_path,
                "config": request.config,
                "instruments": request.instruments,
            },
            idempotency_key=idempotency_key,
        )

        # Auto-link graduation candidate (if one exists in
        # ``live_running`` for this strategy) so the audit trail
        # connects the graduated strategy to its actual deployment.
        try:
            from msai.models.graduation_candidate import GraduationCandidate

            candidate = (
                await db.execute(
                    select(GraduationCandidate).where(
                        GraduationCandidate.strategy_id == request.strategy_id,
                        GraduationCandidate.stage == "live_running",
                        GraduationCandidate.deployment_id.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if candidate is not None:
                candidate.deployment_id = deployment.id
                await db.commit()
                log.info(
                    "graduation_candidate_linked",
                    extra={
                        "candidate_id": str(candidate.id),
                        "deployment_id": str(deployment.id),
                    },
                )
        except Exception:  # noqa: BLE001
            log.warning("graduation_candidate_link_failed", exc_info=True)

        # Register the new deployment with the projection consumer so
        # it discovers the Nautilus message bus stream without a restart.
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
                action="live_start",
                resource_type="live_deployment",
                resource_id=deployment.id,
                details={
                    "instruments": request.instruments,
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

        # Terminal failure branch — classify via FailureKind.
        kind = FailureKind.parse_or_unknown(row.failure_kind)
        if kind is FailureKind.HALT_ACTIVE:
            outcome = EndpointOutcome.halt_active()
            if reservation is not None:
                await idem.release(reservation.redis_key)
            return _apply_outcome(outcome)

        # Codex iter6 P2: SPAWN_FAILED_TRANSIENT means the
        # supervisor's payload factory raised a transient error
        # (Postgres briefly down, network timeout). The command is
        # still in the Redis PEL for XAUTOCLAIM redelivery, so the
        # endpoint must NOT cache a permanent-looking response. A
        # subsequent retry with the same Idempotency-Key should be
        # allowed to re-attempt once the dependency recovers.
        if kind is FailureKind.SPAWN_FAILED_TRANSIENT:
            outcome = EndpointOutcome.spawn_failed_transient(
                row.error_message or "transient supervisor failure"
            )
            if reservation is not None:
                await idem.release(reservation.redis_key)
            return _apply_outcome(outcome)

        # Map unexpected kinds (NONE on a failed row, or endpoint-only
        # values the subprocess wouldn't write) to UNKNOWN so the
        # endpoint doesn't crash.
        permanent_kinds = {
            FailureKind.SPAWN_FAILED_PERMANENT,
            FailureKind.RECONCILIATION_FAILED,
            FailureKind.BUILD_TIMEOUT,
            FailureKind.HEARTBEAT_TIMEOUT,
            FailureKind.UNKNOWN,
        }
        if kind not in permanent_kinds:
            kind = FailureKind.UNKNOWN
        outcome = EndpointOutcome.permanent_failure(kind, row.error_message or "unknown failure")
        if reservation is not None:
            await idem.commit(reservation.redis_key, body_hash, outcome)
        return _apply_outcome(outcome)

    except Exception:
        # Hard failure (raised exception) — release the reservation
        # so the next retry can re-attempt. Re-raise so FastAPI
        # produces the usual 500 (or the HTTPException the caller
        # raised, e.g. strategy 404).
        if reservation is not None:
            await idem.release(reservation.redis_key)
        raise


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
    # Layer 1: HTTP Idempotency-Key reservation
    # ------------------------------------------------------------------
    user_id = await _resolve_user_id(db, claims)
    body_for_hash: dict[str, Any] = {
        "portfolio_revision_id": str(request.portfolio_revision_id),
        "account_id": request.account_id,
        "paper_trading": request.paper_trading,
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

        members: list[LivePortfolioRevisionStrategy] = (
            await db.execute(
                select(LivePortfolioRevisionStrategy)
                .where(
                    LivePortfolioRevisionStrategy.revision_id == revision.id
                )
                .order_by(LivePortfolioRevisionStrategy.order_index)
            )
        ).scalars().all()
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
            await db.execute(
                select(Strategy).where(Strategy.id.in_(strategy_ids))
            )
        ).scalars().all():
            strategies_by_id[strat_row.id] = strat_row

        missing = [sid for sid in strategy_ids if sid not in strategies_by_id]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Strategies not found: {[str(s) for s in missing]}",
            )

        # Compute code hash for the first member (for the deployment row)
        first_strategy = strategies_by_id[members[0].strategy_id]
        strategy_code_hash = _resolve_strategy_code_hash(first_strategy)

        # Aggregate instruments from all members
        all_instruments: list[str] = []
        for m in members:
            all_instruments.extend(m.instruments)
        all_instruments = sorted(set(all_instruments))

        # Aggregate config from all members for the deployment row
        combined_config: dict[str, Any] = {}
        for m in members:
            strat = strategies_by_id[m.strategy_id]
            combined_config[f"{strat.strategy_class}_{m.order_index}"] = m.config

        # -------------------------------------------------------------
        # Layer 3: Identity-based warm-restart upsert
        # -------------------------------------------------------------
        identity = derive_portfolio_deployment_identity(
            user_id=user_id,
            portfolio_revision_id=request.portfolio_revision_id,
            account_id=request.account_id,
            paper_trading=request.paper_trading,
            user_sub=claims.get("sub"),
        )
        identity_signature = identity.signature()

        slug = generate_deployment_slug()
        now = datetime.now(UTC)
        deployment_table = LiveDeployment.__table__

        stmt = pg_insert(deployment_table).values(
            strategy_id=first_strategy.id,
            strategy_code_hash=strategy_code_hash,
            config=combined_config,
            instruments=all_instruments,
            status="starting",
            paper_trading=request.paper_trading,
            last_started_at=now,
            last_stopped_at=None,
            started_by=user_id,
            deployment_slug=slug,
            identity_signature=identity_signature,
            trader_id=derive_trader_id(slug),
            strategy_id_full=derive_strategy_id_full(
                first_strategy.strategy_class, slug
            ),
            account_id=request.account_id,
            ib_login_key=request.ib_login_key,
            message_bus_stream=derive_message_bus_stream(slug),
            config_hash=compute_config_hash(combined_config),
            instruments_signature=",".join(all_instruments),
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

        permanent_kinds = {
            FailureKind.SPAWN_FAILED_PERMANENT,
            FailureKind.RECONCILIATION_FAILED,
            FailureKind.BUILD_TIMEOUT,
            FailureKind.HEARTBEAT_TIMEOUT,
            FailureKind.UNKNOWN,
        }
        if kind not in permanent_kinds:
            kind = FailureKind.UNKNOWN
        outcome = EndpointOutcome.permanent_failure(kind, row.error_message or "unknown failure")
        if reservation is not None:
            await idem.commit(reservation.redis_key, body_hash, outcome)
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

    # Publish the STOP command.
    await bus.publish_stop(
        deployment_id=deployment.id,
        reason="user",
        idempotency_key=idempotency_key,
    )

    # Poll until the supervisor flips the row to stopped / failed.
    row = await _poll_for_terminal(
        db,
        deployment.id,
        ready_statuses=frozenset(),
        terminal_statuses=frozenset({"stopped", "failed"}),
        timeout_s=STOP_POLL_TIMEOUT_S,
        interval_s=START_POLL_INTERVAL_S,
    )

    if row is None:
        return _apply_outcome(EndpointOutcome.api_poll_timeout())

    deployment.status = "stopped"
    deployment.last_stopped_at = datetime.now(UTC)
    await db.commit()

    await log_audit(
        db,
        user_id=deployment.started_by,
        action="live_stop",
        resource_type="live_deployment",
        resource_id=deployment.id,
    )

    log.info(
        "live_deployment_stopped",
        deployment_id=str(deployment.id),
        process_status=row.status,
    )

    return _apply_outcome(
        EndpointOutcome.stopped(
            {
                "id": str(deployment.id),
                "status": "stopped",
                "process_status": row.status,
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
    for row in rows:
        try:
            await bus.publish_stop(row.deployment_id, reason="kill_switch")
            stopped += 1
        except Exception:  # noqa: BLE001
            failed.append(str(row.deployment_id))
            log.exception(
                "kill_switch_publish_stop_failed",
                deployment_id=str(row.deployment_id),
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
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LiveStatusResponse:
    """All deployments with their current status.

    Queries the database for recent deployments and combines that with
    the in-memory node manager status.
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
    result = await db.execute(select(LiveDeployment).order_by(last_activity.desc()).limit(50))
    deployments = result.scalars().all()

    items = [
        LiveDeploymentInfo(
            id=d.id,
            strategy_id=d.strategy_id,
            status=d.status,
            paper_trading=d.paper_trading,
            instruments=d.instruments,
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
        instruments=list(deployment.instruments or []),
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
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> LiveTradesResponse:
    """Recent live trade executions from order_attempt_audits.

    NOTE: Returns ALL live fills, not scoped to the authenticated user.
    Single-operator design — see /positions docstring for rationale.
    """
    from msai.models.order_attempt_audit import OrderAttemptAudit

    count_q = (
        select(func.count())
        .select_from(OrderAttemptAudit)
        .where(
            OrderAttemptAudit.is_live.is_(True),
            OrderAttemptAudit.status.in_(("filled", "partially_filled")),
        )
    )
    total = (await db.execute(count_q)).scalar_one()

    rows_q = (
        select(OrderAttemptAudit)
        .where(
            OrderAttemptAudit.is_live.is_(True),
            OrderAttemptAudit.status.in_(("filled", "partially_filled")),
        )
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
