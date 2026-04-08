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

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from msai.api.live_deps import get_command_bus, get_idempotency_store
from msai.core.audit import log_audit
from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.core.logging import get_logger
from msai.models.live_deployment import LiveDeployment
from msai.models.live_node_process import LiveNodeProcess
from msai.models.strategy import Strategy
from msai.models.user import User
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
)
from msai.services.live.deployment_identity import (
    derive_deployment_identity,
    derive_message_bus_stream,
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
    """Resolve the authenticated user's database ID from JWT claims.

    If the ``users`` row for ``sub`` doesn't exist yet (JWT user calling
    ``/start`` before ``/auth/me`` has provisioned them, or API-key user
    hitting a DB that hadn't finished migrations at startup), provision
    it inline. This is the only way to get a stable per-user
    ``identity_signature`` without introducing a parallel ``sub``-keyed
    identity universe (Codex Task 1.1b iteration 6, P1+P2 fix).

    Concurrency:
    A concurrent insert losing the race is handled inside a SAVEPOINT
    (``db.begin_nested()``). That way the ``IntegrityError`` rollback
    only unwinds the savepoint, NOT the outer transaction — preserving
    any ORM objects the caller already loaded in the session
    (e.g. the ``Strategy`` row fetched by ``live_start`` before calling
    this helper). A plain ``db.rollback()`` would expire every loaded
    ORM object and the caller's subsequent ``strategy.default_config``
    / ``strategy.strategy_class`` access would trigger a lazy refresh
    from the async session context and raise ``MissingGreenlet``
    (Codex Task 1.5 iter2 P2 fix).
    """
    sub = claims.get("sub")
    if not sub:
        return None
    result = await db.execute(select(User.id).where(User.entra_id == sub))
    row_id = result.scalar_one_or_none()
    if row_id is not None:
        return row_id

    email = claims.get("preferred_username") or f"{sub}@unknown.local"
    new_user = User(
        entra_id=str(sub),
        email=str(email),
        display_name=claims.get("name"),
        role="operator",
    )
    try:
        async with db.begin_nested():
            db.add(new_user)
            # flush runs inside the SAVEPOINT so an IntegrityError only
            # unwinds this nested transaction, not the outer session state.
            await db.flush()
    except IntegrityError:
        # Savepoint already rolled back by the context manager. The
        # outer session and all previously-loaded ORM objects are
        # preserved — we just re-read the row the concurrent insert
        # wrote.
        result = await db.execute(select(User.id).where(User.entra_id == sub))
        return result.scalar_one_or_none()
    return new_user.id


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
) -> LivePositionsResponse:
    """Current open positions.

    TODO: Wire to TradingNodeManager position tracking in Phase 2.
    """
    return LivePositionsResponse(positions=[])


@router.get("/trades")
async def live_trades(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> LiveTradesResponse:
    """Recent live trade executions.

    TODO: Query the trades table filtered by ``is_live=True``.
    """
    return LiveTradesResponse(trades=[], total=0)
