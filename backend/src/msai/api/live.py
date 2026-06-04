"""Live trading API router -- deploy, monitor, and control live strategies.

Manages the full lifecycle of live/paper trading deployments: starting
strategies, stopping them, querying status, and emergency halt.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID  # noqa: TC003 — FastAPI resolves the type at runtime for path params

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from msai.api._broker_account_deps import build_broker_account_service
from msai.api.live_deps import get_command_bus, get_idempotency_store
from msai.core.audit import log_audit
from msai.core.auth import get_current_user, resolve_user_id
from msai.core.database import get_db
from msai.core.halt_keys import (
    HALT_TTL_SECONDS,
    HALT_WRITE_LUA,
    RESUME_CLEAR_LUA,
    VERDICT_KEY_SUFFIX,
    HaltCause,
    account_halt_key,
    data_freshness_key,
    data_freshness_manifest_key,
    fleet_halt_key,
    fleet_halt_write_args,
    halt_cause_key,
    reconciled_key,
)
from msai.core.logging import get_logger
from msai.models.broker_account import BrokerAccount
from msai.models.live_deployment import LiveDeployment
from msai.models.live_deployment_strategy import LiveDeploymentStrategy
from msai.models.live_node_process import LiveNodeProcess
from msai.models.live_portfolio_revision import LivePortfolioRevision
from msai.models.live_portfolio_revision_strategy import LivePortfolioRevisionStrategy
from msai.models.strategy import Strategy
from msai.schemas.live import (
    DataHealthResponse,
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
    ResumeVerifiedPreconditions,
)
from msai.services.live.broker_account_service import ACTIVE_DEPLOYMENT_STATUSES
from msai.services.live.deployment_account_resolver import (
    AccountNotResolvable,
    lock_and_assert_account_active,
    resolve_active_broker_account,
    validate_account_credentials,
    validate_account_row_state,
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
    ROUTER_HEARTBEAT_KEY,
    LiveCommandBus,  # noqa: TC001 — FastAPI Depends resolves at runtime
)
from msai.services.nautilus.ibg_client_id import derive_ibg_client_id
from msai.services.nautilus.trading_node import TradingNodeManager
from msai.services.observability.broker_account_metrics import (
    DEPLOY_VALIDATION_FAILED,
    KV_SECRET_AGE,
)
from msai.services.risk_engine import RiskEngine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from msai.models.graduation_candidate import GraduationCandidate
    from msai.services.live.gateway_router import GatewayRouter

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


_HALT_KEY = fleet_halt_key()
"""Redis key checked by ``/start`` (layer 2 of the three-layer
idempotency model — decision #16). Set by ``/kill-all``. Consolidated
into ``msai.core.halt_keys`` via PR 1 T2."""


# ---------------------------------------------------------------------------
# Shared helpers for the start/stop path
# ---------------------------------------------------------------------------


async def _halt_is_active(bus: LiveCommandBus) -> bool:
    """Read the halt flag from Redis via the command bus's client.

    Tests stub the bus's ``_redis`` attribute directly so this path
    is exercised without the global ``get_command_bus`` dependency.
    """
    return bool(await bus._redis.exists(_HALT_KEY))  # noqa: SLF001 — intentional


async def _account_halt_is_active(bus: LiveCommandBus, account_id: str) -> bool:
    """Read the account-scoped halt latch from Redis.

    PR 1 T8 / Codex iter 1 P2-1 fix: the ``/drain/{account_id}``
    endpoint sets ``account_halt_key(account_id)`` to block the
    drained account, but ``/start-portfolio`` previously ignored that
    key — a queued or operator-initiated start could spawn the
    drained account immediately after a drain. This helper closes
    the gap. Empty ``account_id`` → False (callers that don't yet
    set ``account_id`` keep working).
    """
    if not account_id:
        return False
    return bool(await bus._redis.exists(account_halt_key(account_id)))  # noqa: SLF001


# Max age of the supervisor's ``router_heartbeat`` before the
# ``/start-portfolio`` 503 gate treats the supervisor as dead. The
# supervisor stamps the heartbeat every ~5 s (``run_forever``'s
# ``ROUTER_HEARTBEAT_PUBLISH_INTERVAL_S``); 15 s is 3× that, so a
# brief event-loop stall doesn't false-trip the gate while a genuinely
# crashed/stopped supervisor is detected within one poll window.
_SUPERVISOR_MAX_HEARTBEAT_AGE_S = 15.0


async def _supervisor_is_alive(bus: LiveCommandBus) -> bool:
    """Return True when the supervisor's ``router_heartbeat`` is fresh.

    Drill 2026-04-15 P0-A: the ``live-supervisor`` service is gated
    behind the ``broker`` compose profile and therefore absent from
    the default ``docker compose up`` stack. When the supervisor is
    down, ``/api/v1/live/start`` used to publish a command, poll the
    (never-created) ``live_node_processes`` row until its 60 s
    deadline, and return 504 — a silent hang with no actionable
    error. Gating on supervisor liveness here lets the endpoint return
    503 with a clear remediation message the moment an operator forgets
    to activate the profile or the supervisor has crashed.

    PR 2 T4: this probe MUST read the ``router_heartbeat`` Redis key,
    NOT the global command stream's consumer-group activity. T4 retires
    the single global ``bus.consume()`` and fans the supervisor out into
    per-account consumers; ``xinfo_consumers(GLOBAL_STREAM)`` would then
    show no active consumer and this gate would 503 EVERY
    ``/start-portfolio`` even with the supervisor up. Reading the
    heartbeat key (which ``run_forever`` stamps every loop pass) is
    stream-topology-independent. ``None`` age (key absent/expired) ⇒
    fail-closed (dead).
    """
    try:
        age = await bus.read_router_heartbeat_age_s()
    except Exception as exc:  # noqa: BLE001 — any Redis error means "can't tell, assume dead"
        # iter-4 SF P3: log with type so a programming bug (wrong key
        # name, redis-py API change) doesn't manifest as "supervisor
        # permanently dead" with no forensic trail.
        log.warning(
            "supervisor_liveness_check_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False
    if age is None:
        return False
    return age < _SUPERVISOR_MAX_HEARTBEAT_AGE_S


def _heartbeat_age_s(last_heartbeat_at: datetime | None) -> float | None:
    """Return the age in seconds of *last_heartbeat_at*, or ``None`` when
    absent. Server-side so the UI/CLI don't have to clock-compare.

    A naive (tz-unaware) timestamp is treated as UTC — the column is
    ``DateTime(timezone=True)`` so this only matters for defensive safety.
    Clamped at 0 so a tiny clock skew never yields a negative age.
    """
    if last_heartbeat_at is None:
        return None
    ts = last_heartbeat_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - ts).total_seconds())


def _restart_authority_fields(
    process: LiveNodeProcess | None,
    *,
    fleet_halted: bool,
    account_halted: bool,
) -> dict[str, Any]:
    """Build the additive PR 2 T8 restart-authority field dict for a
    deployment's status row, from its latest ``live_node_processes`` row
    (may be ``None`` when the deployment never spawned a node) + the live
    fleet/account halt-latch booleans (read from Redis by the caller).

    Shared by the list (``/live/status``) and detail
    (``/live/status/{deployment_id}``) endpoints so the two never drift —
    UC-API-1 requires the detail GET to carry the SAME fields.
    """
    return {
        "auto_restart_paused": (process.auto_restart_paused if process else None),
        "auto_restart_pause_reason": (process.auto_restart_pause_reason if process else None),
        "consecutive_respawn_failures": (process.consecutive_respawn_failures if process else None),
        "last_restart_at": (process.last_restart_at if process else None),
        "last_heartbeat_age_s": (_heartbeat_age_s(process.last_heartbeat_at) if process else None),
        "fleet_halted": fleet_halted,
        "account_halted": account_halted,
    }


async def _latest_process_by_deployment(
    db: AsyncSession, deployment_ids: list[UUID]
) -> dict[UUID, LiveNodeProcess]:
    """Return the most-recent ``live_node_processes`` row per deployment.

    Single query (``DISTINCT ON (deployment_id) ... ORDER BY deployment_id,
    started_at DESC``) so the list endpoint stays O(1) queries — no N+1
    per-deployment fetch. Deployments with no process row are simply absent
    from the returned map (the caller maps them to ``None``).
    """
    if not deployment_ids:
        return {}
    rows = (
        (
            await db.execute(
                select(LiveNodeProcess)
                .where(LiveNodeProcess.deployment_id.in_(deployment_ids))
                .order_by(
                    LiveNodeProcess.deployment_id,
                    LiveNodeProcess.started_at.desc(),
                )
                .distinct(LiveNodeProcess.deployment_id)
            )
        )
        .scalars()
        .all()
    )
    return {p.deployment_id: p for p in rows}


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


# The live_node_processes statuses that mean "a trading subprocess is alive
# (or building toward alive) RIGHT NOW for this deployment". Matches the
# partial unique index ``uq_live_node_processes_active_deployment`` on the
# model so the API and the DB agree on what "active" means.
_ACTIVE_NODE_PROCESS_STATUSES: frozenset[str] = frozenset(
    {"starting", "building", "ready", "running", "stopping"}
)


async def _active_node_process_exists(db: AsyncSession, deployment_id: UUID) -> bool:
    """Return True if an ACTIVE ``live_node_processes`` row exists for
    ``deployment_id`` (PR 2 T4 review P1 — stranded-START flip guard).

    The ``/start-portfolio`` poll-timeout flip must NOT mark a deployment
    ``failed`` when a trading subprocess is in fact alive but slow to build /
    reconcile (IB connect + reconciliation legitimately take time — nautilus
    gotchas #10 / #19, and the supervisor watchdog tolerates a wedged build
    for ``startup_hard_timeout_s`` = 1800s, far beyond the 60s API poll).

    Flipping the deployment to ``failed`` while its node row is still
    ``building`` would make that real-money node INVISIBLE to
    ``GET /live/status?active_only=true`` AND to the production deploy gate
    (which selects ``status IN ('starting','building','ready','running')``),
    so a routine push-to-main deploy could recreate the supervisor/containers
    while a real-money node is live and unaccounted-for — the exact failure
    PR 2's binding deploy contract exists to prevent.

    So the flip is allowed ONLY when this returns False (no active row → the
    START genuinely stranded: no per-account consumer attached, nothing
    spawned). A fresh DB read is used (the caller already rolled back inside
    the poll loop) so we observe rows the supervisor committed from another
    session.
    """
    row = (
        await db.execute(
            select(LiveNodeProcess.id)
            .where(
                LiveNodeProcess.deployment_id == deployment_id,
                LiveNodeProcess.status.in_(_ACTIVE_NODE_PROCESS_STATUSES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


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


@dataclass(frozen=True)
class _EffectiveAccount:
    """The account identity ALL downstream safety/identity checks must use.

    ``account_id`` / ``ib_login_key`` are the EFFECTIVE strings — DERIVED from
    the resolved :class:`BrokerAccount` row when one was resolved, or the legacy
    request strings on the warm-restart back-compat path. ``broker_account`` is
    the resolved registry row (``None`` on the legacy warm-restart path, where
    no resolution is forced and the account may be unregistered).

    Once an ``_EffectiveAccount`` is established at the TOP of the handler, the
    raw ``request.account_id`` must NEVER drive a safety check (identity,
    body-hash, collision, per-account halt, publish) — that would re-open the
    halt-bypass an attacker could ride by sending a halted derived account
    alongside a non-halted raw ``account_id``.
    """

    account_id: str
    ib_login_key: str
    broker_account: BrokerAccount | None


def _deploy_error(status_code: int, code: str, message: str, details: dict[str, Any]) -> NoReturn:
    """Raise an :class:`HTTPException` carrying the canonical ``{"error": {...}}``
    envelope used across the deploy path. Single builder so the status code /
    error code / message / details contract (asserted by the integration tests)
    is constructed in exactly one place. Does NOT touch the alert metric — callers
    that need to count a rejection do so before calling this (see
    :func:`_not_resolvable_fail` / :func:`_deploy_validation_fail`)."""
    raise HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details}},
    )


def _not_resolvable_fail(account_id: str, message: str, details: dict[str, Any]) -> NoReturn:
    """Increment the alert metric + raise a 422 ``BROKER_ACCOUNT_NOT_RESOLVABLE``.

    Wraps :func:`_deploy_error` for the three credential-resolvability sites in
    ``_resolve_effective_account`` / ``_resolve_new_legacy_deploy_account``, all
    of which count the rejection with ``reason="not_resolvable"`` and surface the
    same error code."""
    DEPLOY_VALIDATION_FAILED.inc(account_id=account_id, reason="not_resolvable")
    _deploy_error(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "BROKER_ACCOUNT_NOT_RESOLVABLE",
        message,
        details,
    )


def _deploy_validation_fail(account_id: str, reason: str | None) -> NoReturn:
    """Increment the alert metric + raise a fail-closed 422 for a row-state
    deploy-validation rejection (archived / mode_inconsistent / route_not_found
    / not_router_bound). Credential-resolvability failures are NOT routed here —
    those are counted inside ``resolve_for_spawn`` and surfaced separately."""
    DEPLOY_VALIDATION_FAILED.inc(account_id=account_id, reason=reason or "unknown")
    _deploy_error(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "DEPLOY_VALIDATION_FAILED",
        (
            f"Broker account {account_id} failed deploy validation: {reason}. "
            "Resolve the account state (un-archive / fix mode / bind the gateway "
            "route) before deploying."
        ),
        {"account_id": account_id, "reason": reason},
    )


async def _resolve_effective_account(
    *,
    db: AsyncSession,
    request: PortfolioStartRequest,
    user_id: UUID | None,
    claims: dict[str, Any],
    gateway_router: GatewayRouter,
) -> _EffectiveAccount:
    """Establish the EFFECTIVE account at the TOP of ``/start-portfolio``.

    Resolution rules (council mandate — new free-form deploys must resolve or
    fail closed; warm restarts of pre-registry deployments stay back-compatible):

    * ``broker_account_id`` set → resolve the registry row by id (422 on
      :class:`AccountNotResolvable`). Effective strings are DERIVED from the row.
    * legacy strings only → compute the warm-restart identity from the REQUEST
      strings. If an EXISTING deployment matches that identity_signature:
        - its row's ``broker_account_id IS NULL`` (never linked to the registry,
          predates it) → keep the legacy strings, no forced resolution; OR
        - its row's ``broker_account_id IS NOT NULL`` (LINKED to a registry
          account) → the legacy bypass is NOT allowed: resolve that linked
          account by its row id and run validation, so a since-archived /
          invalidated broker-linked deployment fails closed (422) instead of
          being grandfathered back to life.
      Otherwise this is a NEW deploy: resolve by ``ib_account_id``
      (422 on :class:`AccountNotResolvable`) and DERIVE the effective strings.

    When an account is resolved, the CHEAP row-state validation
    (:func:`validate_account_row_state`) runs here (no KV, no commit); an invalid
    row fails closed with 422 + an alert-metric increment. The KV credential
    validation runs LATER (after the idempotency reservation) in the handler.
    """
    requested_mode = "paper" if request.paper_trading else "live"

    account: BrokerAccount | None = None
    if request.broker_account_id is not None:
        try:
            account = await resolve_active_broker_account(
                db, broker_account_id=request.broker_account_id, ib_account_id=None
            )
        except AccountNotResolvable as exc:
            # An unresolvable id (unknown OR only an ARCHIVED row exists for it)
            # is an alertable deploy rejection — count it (label by the id) so
            # an operator deploying against a stale/archived account is visible.
            try:
                _not_resolvable_fail(
                    str(request.broker_account_id),
                    str(exc),
                    {"broker_account_id": str(request.broker_account_id)},
                )
            except HTTPException as http_exc:
                raise http_exc from exc
    else:
        # Legacy strings path. The either/or schema validator guarantees both
        # account_id + ib_login_key are present here.
        assert request.account_id is not None  # noqa: S101 — schema-guaranteed
        assert request.ib_login_key is not None  # noqa: S101 — schema-guaranteed
        identity = derive_portfolio_deployment_identity(
            user_id=user_id,
            portfolio_revision_id=request.portfolio_revision_id,
            account_id=request.account_id,
            paper_trading=request.paper_trading,
            ib_login_key=request.ib_login_key,
            user_sub=claims.get("sub"),
        )
        existing = (
            await db.execute(
                select(
                    LiveDeployment.id,
                    LiveDeployment.broker_account_id,
                ).where(LiveDeployment.identity_signature == identity.signature())
            )
        ).one_or_none()
        if existing is not None:
            existing_broker_account_id = existing.broker_account_id
            if existing_broker_account_id is None:
                # Warm-restart of a row that was NEVER linked to a registry account
                # (predates the registry FK). Back-compat — keep the legacy strings
                # with no forced resolution — is preserved ONLY while NO
                # BrokerAccount exists for this IB account. If an operator has SINCE
                # registered one for this account_id, the registry is authoritative
                # and the NULL-FK restart must NOT bypass it (Codex iter-20 P2):
                # resolve it so an ACTIVE row links + validates and an
                # archived/inactive row fails closed (422) — closing the
                # "spawn against a since-archived account via a legacy restart" hole.
                registry_row_exists = (
                    await db.execute(
                        select(BrokerAccount.id)
                        .where(BrokerAccount.ib_account_id == request.account_id)
                        .limit(1)
                    )
                ).first() is not None
                if not registry_row_exists:
                    # Genuinely pre-registry account_id → keep the legacy strings.
                    return _EffectiveAccount(
                        account_id=request.account_id,
                        ib_login_key=request.ib_login_key,
                        broker_account=None,
                    )
                # A BrokerAccount exists for this IB account → resolve it (ACTIVE
                # links + validates; archived/inactive → 422 fail closed).
                try:
                    account = await resolve_active_broker_account(
                        db, broker_account_id=None, ib_account_id=request.account_id
                    )
                except AccountNotResolvable as exc:
                    try:
                        _not_resolvable_fail(
                            str(request.account_id),
                            (
                                f"{exc} A broker account is registered for this IB "
                                "account but is no longer active (archived or "
                                "invalidated). Restore/replace it before restarting."
                            ),
                            {"account_id": request.account_id},
                        )
                    except HTTPException as http_exc:
                        raise http_exc from exc
            else:
                # The matched row IS linked to a BrokerAccount. A legacy-strings
                # restart must NOT bypass validation: resolve that linked account
                # by its row id and fall through to row-state validation below so a
                # since-archived / invalidated account fails closed (no
                # grandfathering a broker-linked deployment back to life).
                try:
                    account = await resolve_active_broker_account(
                        db,
                        broker_account_id=existing_broker_account_id,
                        ib_account_id=None,
                    )
                except AccountNotResolvable as exc:
                    try:
                        _not_resolvable_fail(
                            str(existing_broker_account_id),
                            (
                                f"{exc} This deployment is linked to a broker account "
                                "that is no longer active (archived or invalidated). "
                                "Restore/replace the account before restarting."
                            ),
                            {"broker_account_id": str(existing_broker_account_id)},
                        )
                    except HTTPException as http_exc:
                        raise http_exc from exc
            # account is set (broker-linked restart OR NULL-FK restart whose IB
            # account now has a registry row) → fall through to the shared
            # row-state validation + effective-account derivation below.
        else:
            # NEW free-form deploy: must resolve to an ACTIVE registry row or fail.
            account = await _resolve_new_legacy_deploy_account(db, request)

    # An account was resolved (by id, by ib_account_id on a new deploy, or via
    # an existing broker-linked row on a legacy-strings restart). Run the CHEAP
    # row-state validation now — fail closed on an invalid row.
    row_state = validate_account_row_state(
        account, requested_mode=requested_mode, gateway_router=gateway_router
    )
    if not row_state.valid:
        _deploy_validation_fail(account.ib_account_id, row_state.reason)

    return _EffectiveAccount(
        account_id=account.ib_account_id,
        ib_login_key=account.ib_login_key,
        broker_account=account,
    )


async def _resolve_new_legacy_deploy_account(
    db: AsyncSession, request: PortfolioStartRequest
) -> BrokerAccount:
    """Resolve a NEW free-form (legacy-strings) deploy to an ACTIVE registry row.

    Council mandate: new free-form deploys must resolve or fail closed — they
    are NOT silently legacy-passed-through. Raises 422
    ``BROKER_ACCOUNT_NOT_RESOLVABLE`` (and increments the alert metric) when the
    request ``account_id`` does not match an ACTIVE registry row.
    """
    assert request.account_id is not None  # noqa: S101 — schema-guaranteed
    try:
        return await resolve_active_broker_account(
            db, broker_account_id=None, ib_account_id=request.account_id
        )
    except AccountNotResolvable as exc:
        try:
            _not_resolvable_fail(
                str(request.account_id),
                (
                    f"{exc} New deployments must reference an ACTIVE broker "
                    "account (register it via POST /api/v1/broker-accounts "
                    "first)."
                ),
                {"account_id": request.account_id},
            )
        except HTTPException as http_exc:
            raise http_exc from exc


async def _resolve_binding_for_start_portfolio(
    *,
    db: AsyncSession,
    request: PortfolioStartRequest,
    effective_account_id: str,
    effective_ib_login_key: str,
    user_id: UUID | None,
    claims: dict[str, Any],
) -> tuple[
    str,
    list[tuple[LivePortfolioRevisionStrategy, GraduationCandidate]],
    list[tuple[list[str], list[str]]],
]:
    """Pre-reserve snapshot binding (Bug #3, replaces PR #63's 503
    guard). Loads the frozen revision + members, resolves each
    member's bound :class:`GraduationCandidate`, verifies
    config-minus-injected + instruments match, computes a stable
    binding fingerprint, returns it for inclusion in the idempotency
    body_hash + the resolved (member, candidate) pairs for downstream
    stage-link logic.

    Raises ``HTTPException`` 422 on any binding failure:

    - ``BINDING_REVISION_NOT_FOUND`` — bad request, no such revision
    - ``LIVE_DEPLOY_REPAIR_REQUIRED`` — warm restart but candidate
      vanished after first deploy (e.g. archived)
    - ``BINDING_NOT_GRADUATED`` — first deploy but no live_candidate
    - ``BINDING_AMBIGUOUS`` — multiple live_candidates for same strategy
    - ``BINDING_INELIGIBLE`` — linked candidate drifted to archived
    - ``BINDING_MISMATCH`` — config or instruments diverge
    - ``BINDING_INSTRUMENTS_MISSING`` — pre-PR-#65 candidate without
      instruments stamp (operator must re-graduate / backfill)

    See ``docs/plans/2026-05-13-live-deploy-safety-trio.md`` §Bug #3.
    """
    from msai.models.graduation_candidate import GraduationCandidate
    from msai.services.graduation import ELIGIBLE_FOR_LIVE_PORTFOLIO
    from msai.services.live.snapshot_binding import (
        BindingInstrumentsMissingError,
        BindingMismatchError,
        candidate_instruments,
        compute_binding_fingerprint,
        compute_member_fingerprint,
        verify_member_matches_candidate,
    )

    # ----- 1. Identity signature (for warm-restart lookup) -----
    # Task 5: identity is built from the EFFECTIVE account (derived from the
    # resolved BrokerAccount row, or the legacy strings on warm restart) — NOT
    # the raw request strings, so the binding warm-restart lookup + the
    # collision check below agree with the handler's identity recompute.
    identity = derive_portfolio_deployment_identity(
        user_id=user_id,
        portfolio_revision_id=request.portfolio_revision_id,
        account_id=effective_account_id,
        paper_trading=request.paper_trading,
        ib_login_key=effective_ib_login_key,
        user_sub=claims.get("sub"),
    )
    identity_signature = identity.signature()

    # ----- 2. Existing deployment lookup (warm restart) -----
    existing_deployment = (
        await db.execute(
            select(LiveDeployment).where(LiveDeployment.identity_signature == identity_signature)
        )
    ).scalar_one_or_none()
    existing_deployment_id = existing_deployment.id if existing_deployment is not None else None

    # ----- 2b. Codex round-3 P2: surface (revision_id, account_id)
    # collisions BEFORE the binding lookup. If an existing deployment
    # under a DIFFERENT identity already linked the only live_candidate,
    # the first-deploy branch below would return BINDING_NOT_GRADUATED
    # and operators would be told to re-graduate when the correct
    # remediation is to stop/archive the colliding deployment. Mirrors
    # the gate further down inside the handler so the error surfaces
    # at the binding layer too.
    if existing_deployment_id is None:
        collision_row = (
            await db.execute(
                select(LiveDeployment).where(
                    LiveDeployment.portfolio_revision_id == request.portfolio_revision_id,
                    LiveDeployment.account_id == effective_account_id,
                    LiveDeployment.identity_signature != identity_signature,
                )
            )
        ).scalar_one_or_none()
        if collision_row is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "LIVE_DEPLOY_CONFLICT",
                        "message": (
                            "An existing deployment for this portfolio revision + account "
                            "exists with a different identity (different ib_login_key, "
                            "paper_trading, or other identity-bearing field). Archive/stop "
                            "the existing row OR re-submit with the same identity."
                        ),
                        "details": {
                            "existing_deployment_id": str(collision_row.id),
                            "existing_status": collision_row.status,
                            "existing_ib_login_key": collision_row.ib_login_key,
                            "existing_paper_trading": collision_row.paper_trading,
                            # Use the EFFECTIVE login (derived from the resolved
                            # BrokerAccount on a selector-only deploy) — the raw
                            # ``request.ib_login_key`` is None when the caller
                            # selected by ``broker_account_id``.
                            "requested_ib_login_key": effective_ib_login_key,
                            "requested_paper_trading": request.paper_trading,
                            "hint": (
                                "stop the existing deployment via POST /api/v1/live/stop, "
                                "then retry"
                            ),
                        },
                    }
                },
            )

    # ----- 3. Load frozen revision + members (no lock — binding is a
    # read-only check; the actual deploy below takes SELECT FOR UPDATE).
    revision = (
        await db.execute(
            select(LivePortfolioRevision).where(
                LivePortfolioRevision.id == request.portfolio_revision_id
            )
        )
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Portfolio revision {request.portfolio_revision_id} not found",
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
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": {
                    "code": "BINDING_EMPTY_REVISION",
                    "message": "Frozen revision has no strategy members; cannot deploy.",
                }
            },
        )

    # ----- 4. Per-member candidate resolution -----
    resolved_bindings: list[tuple[LivePortfolioRevisionStrategy, GraduationCandidate]] = []
    canonical_per_member: list[tuple[list[str], list[str]]] = []
    # Codex round-4 P2: the deployment row is committed BEFORE the
    # candidate gets linked (see link block below). A retry that lands
    # inside the upsert→link window — or after a previous attempt failed
    # at link time and left an unlinked row marked `failed`/`starting` —
    # would otherwise hit the warm-restart branch and incorrectly raise
    # LIVE_DEPLOY_REPAIR_REQUIRED. Treat unlinked rows for the same
    # identity as RETRYABLE: fall through to the first-deploy lookup
    # path. The link-block P1 race guard (`locked.deployment_id !=
    # deployment.id`) prevents cross-deployment rebinding even if a
    # candidate later attaches mid-flight.
    existing_unlinked_retry = existing_deployment is not None and existing_deployment.status in (
        "starting",
        "failed",
        "stopped",
    )
    for member in members:
        candidate: GraduationCandidate | None = None
        if existing_deployment_id is not None:
            # Warm restart: deterministic — candidate must be linked to
            # this deployment from a prior successful start.
            candidate = (
                await db.execute(
                    select(GraduationCandidate).where(
                        GraduationCandidate.strategy_id == member.strategy_id,
                        GraduationCandidate.deployment_id == existing_deployment_id,
                    )
                )
            ).scalar_one_or_none()
            if candidate is None and not existing_unlinked_retry:
                # Codex iter-4 P1 #4: do NOT fall back to the first-deploy
                # query when this looks like a successful prior deploy
                # whose linked candidate was archived — operator must
                # repair. Codex round-4 P2: the carve-out is for prior
                # deploys that NEVER completed linking (status in
                # {starting, failed, stopped}) — those retry through
                # the first-deploy lookup below; the link-block P1 race
                # guard prevents cross-deployment rebinding.
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": {
                            "code": "LIVE_DEPLOY_REPAIR_REQUIRED",
                            "message": (
                                f"Existing deployment {existing_deployment_id} has no "
                                f"linked GraduationCandidate for strategy "
                                f"{member.strategy_id}. Likely the candidate was "
                                f"archived after first deploy. Operator must restore "
                                f"the candidate OR archive the deployment row before "
                                f"re-deploying."
                            ),
                            "details": {
                                "existing_deployment_id": str(existing_deployment_id),
                                "strategy_id": str(member.strategy_id),
                                "existing_deployment_status": (
                                    existing_deployment.status
                                    if existing_deployment is not None
                                    else None
                                ),
                            },
                        }
                    },
                )

        if candidate is None:
            # First deploy OR retryable unlinked existing deployment:
            # exactly one candidate at live_candidate with no link yet.
            matched = list(
                (
                    await db.execute(
                        select(GraduationCandidate).where(
                            GraduationCandidate.strategy_id == member.strategy_id,
                            GraduationCandidate.stage == "live_candidate",
                            GraduationCandidate.deployment_id.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if len(matched) == 0:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": {
                            "code": "BINDING_NOT_GRADUATED",
                            "message": (
                                f"Strategy {member.strategy_id} has no GraduationCandidate "
                                f"at stage `live_candidate` with deployment_id=NULL. "
                                f"Walk the candidate through the graduation pipeline first."
                            ),
                            "details": {"strategy_id": str(member.strategy_id)},
                        }
                    },
                )
            if len(matched) > 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "error": {
                            "code": "BINDING_AMBIGUOUS",
                            "message": (
                                f"Strategy {member.strategy_id} has {len(matched)} "
                                f"live_candidate rows — operator must archive duplicates "
                                f"or define an explicit tie-breaker."
                            ),
                            "details": {
                                "strategy_id": str(member.strategy_id),
                                "candidate_ids": [str(c.id) for c in matched],
                            },
                        }
                    },
                )
            candidate = matched[0]

        # ----- 5. Eligibility guard (Codex iter-4 P1 #5).
        if candidate.stage not in ELIGIBLE_FOR_LIVE_PORTFOLIO:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "BINDING_INELIGIBLE",
                        "message": (
                            f"Candidate {candidate.id} is at stage `{candidate.stage}`, "
                            f"not in ELIGIBLE_FOR_LIVE_PORTFOLIO. Re-graduate or pick "
                            f"a different candidate."
                        ),
                        "details": {
                            "candidate_id": str(candidate.id),
                            "stage": candidate.stage,
                            "eligible_stages": sorted(ELIGIBLE_FOR_LIVE_PORTFOLIO),
                        },
                    }
                },
            )

        # ----- 6. Canonicalize instruments via the LIVE resolver
        # (lookup_for_live) so futures rolls + alias drift don't
        # false-reject equivalent symbols. Plan §step 2 instruments
        # canonicalization — Codex round-1 P2 fix. The resolver is
        # pure-registry-read, no IB qualification.
        from msai.services.nautilus.live_instrument_bootstrap import (
            exchange_local_today,
        )
        from msai.services.nautilus.security_master.live_resolver import (
            LiveResolverError,
            lookup_for_live,
        )

        # PR round-2 P2: extract candidate instruments via a guarded call
        # so a pre-contract candidate (missing `config["instruments"]`)
        # surfaces as 422 BINDING_INSTRUMENTS_MISSING — not a 500.
        try:
            cand_raw_instruments = candidate_instruments(candidate)
        except BindingInstrumentsMissingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "BINDING_INSTRUMENTS_MISSING",
                        "message": str(exc),
                        "details": {
                            "candidate_id": str(candidate.id),
                            "hint": (
                                "run `scripts/backfill_candidate_instruments.py` to "
                                "stamp `instruments` into existing pre-Bug-#3 candidates, "
                                "OR re-graduate this candidate."
                            ),
                        },
                    }
                },
            ) from exc

        as_of = exchange_local_today()
        try:
            member_canonical = [
                r.canonical_id
                for r in await lookup_for_live(
                    list(member.instruments), as_of_date=as_of, session=db
                )
            ]
            candidate_canonical = [
                r.canonical_id
                for r in await lookup_for_live(cand_raw_instruments, as_of_date=as_of, session=db)
            ]
        except LiveResolverError as exc:
            # PR round-2 P3: surface the typed resolver envelope (registry
            # code, missing symbols, conflicts, as_of_date) so the operator
            # gets the same diagnostic shape the supervisor surfaces.
            envelope = exc.to_error_message() if hasattr(exc, "to_error_message") else {}
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "BINDING_INSTRUMENT_RESOLVE_FAILED",
                        "message": str(exc),
                        "details": {
                            "member_instruments": list(member.instruments),
                            "candidate_instruments": cand_raw_instruments,
                            "resolver_envelope": envelope,
                        },
                    }
                },
            ) from exc

        # ----- 7. Verify binding using canonical instruments.
        try:
            verify_member_matches_candidate(
                member,
                candidate,
                member_instruments_canonical=member_canonical,
                candidate_instruments_canonical=candidate_canonical,
            )
        except BindingMismatchError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "BINDING_MISMATCH",
                        "message": str(exc),
                        "details": {
                            "member_id": str(member.id),
                            "candidate_id": str(candidate.id),
                            "mismatches": exc.mismatches,
                        },
                    }
                },
            ) from exc
        except BindingInstrumentsMissingError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": {
                        "code": "BINDING_INSTRUMENTS_MISSING",
                        "message": str(exc),
                        "details": {
                            "candidate_id": str(candidate.id),
                            "hint": (
                                "run `scripts/backfill_candidate_instruments.py` to "
                                "stamp `instruments` into existing pre-Bug-#3 "
                                "candidates, OR re-graduate this candidate."
                            ),
                        },
                    }
                },
            ) from exc

        resolved_bindings.append((member, candidate))
        canonical_per_member.append((member_canonical, candidate_canonical))

    # ----- 8. Compute binding fingerprint using LIVE-resolved
    # canonical instrument lists (Codex round-1 P2). The fingerprint
    # must use the SAME canonical form the binding verification used
    # — otherwise futures rolls would produce different fingerprints
    # on every effective-date boundary.
    member_parts: list[str] = []
    for (member, candidate), (m_canon, c_canon) in zip(
        resolved_bindings, canonical_per_member, strict=True
    ):
        member_parts.append(
            compute_member_fingerprint(
                member_id=str(member.id),
                member_config=member.config,
                member_instruments_canonical=m_canon,
                candidate_id=str(candidate.id),
                candidate_config=candidate.config,
                candidate_instruments_canonical=c_canon,
            )
        )
    binding_fingerprint = compute_binding_fingerprint(member_parts)
    return binding_fingerprint, resolved_bindings, canonical_per_member


@router.post("/start-portfolio")
async def live_start_portfolio(  # noqa: PLR0912, PLR0915 — multi-branch dispatch by design
    request: PortfolioStartRequest,
    http_request: Request,
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
    # Layer 0: Pre-reserve snapshot binding (Bug #3, replaces the
    # PR #63 temporary 503 LIVE_DEPLOY_BLOCKED guard). For each frozen
    # member, resolve the bound GraduationCandidate and verify
    # `config` (minus deploy-injected fields) + `instruments` (as a
    # sorted set) match. Compute a `binding_fingerprint` that folds
    # into the idempotency body_hash so candidate drift (re-graduate,
    # archive) invalidates cached outcomes naturally.
    #
    # MUST fire BEFORE idem.reserve(...) below — see plan §Bug #3 step 5
    # "Problem A (cache bypass)".
    # ------------------------------------------------------------------
    user_id = await _resolve_user_id(db, claims)

    # ------------------------------------------------------------------
    # Task 5: establish the EFFECTIVE account BEFORE identity / body-hash /
    # binding. resolve → derive → (cheap row-state validate). From here on the
    # raw ``request.account_id`` must NOT drive any safety / identity check —
    # ``effective_account_id`` / ``effective_ib_login_key`` are authoritative
    # (closes the halt-bypass P1). The KV credential validation runs LATER,
    # after the idempotency reservation, so a replay doesn't re-poke Key Vault.
    # ------------------------------------------------------------------
    gateway_router: GatewayRouter = http_request.app.state.gateway_router
    effective = await _resolve_effective_account(
        db=db,
        request=request,
        user_id=user_id,
        claims=claims,
        gateway_router=gateway_router,
    )
    effective_account_id = effective.account_id
    effective_ib_login_key = effective.ib_login_key

    (
        binding_fingerprint,
        resolved_bindings,
        canonical_per_member,
    ) = await _resolve_binding_for_start_portfolio(
        db=db,
        request=request,
        effective_account_id=effective_account_id,
        effective_ib_login_key=effective_ib_login_key,
        user_id=user_id,
        claims=claims,
    )

    body_for_hash: dict[str, Any] = {
        "portfolio_revision_id": str(request.portfolio_revision_id),
        "account_id": effective_account_id,
        "paper_trading": request.paper_trading,
        "ib_login_key": effective_ib_login_key,
        "binding_fingerprint": binding_fingerprint,
        # P2-B: fold in the RAW account selector (the distinguishing input), not
        # the derived effective account. Two requests that resolve to the same
        # effective account/login but differ in HOW they selected it — one via
        # the legacy ``account_id``/``ib_login_key`` pair (broker_account_id
        # absent → None), the other via the registry ``broker_account_id`` — must
        # NOT collide on the same Idempotency-Key. Without this, a cached legacy
        # warm-restart (no link) could be served to a later selector-bearing
        # retry BEFORE its credential validation + broker-link upsert run, so the
        # link + validation stamps would never persist.
        "broker_account_id": (
            str(request.broker_account_id) if request.broker_account_id is not None else None
        ),
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
        # Layer 2b: Account-scoped halt latch (PR 1 T8 / Codex iter 1 P2-1)
        # -------------------------------------------------------------
        # ``/drain/{account_id}`` writes ``account_halt_key(account_id)``
        # to block the drained sub-account. Reject the start while the
        # latch is set — operator must explicitly clear it before this
        # account can deploy again. Other accounts under the same TWS
        # login are unaffected (latch is keyed by account_id, not
        # ib_login_key — council 2026-05-29 obj #11).
        # Task 5: use the EFFECTIVE account (derived from the resolved row, or
        # the legacy strings on warm restart) — NOT the raw request.account_id.
        # Driving this gate off the raw request would let a halted derived
        # account be bypassed by sending a non-halted raw account_id alongside
        # ``broker_account_id`` (the halt-bypass P1).
        if await _account_halt_is_active(bus, effective_account_id):
            outcome = EndpointOutcome.account_halt_active(effective_account_id)
            if reservation is not None:
                await idem.release(reservation.redis_key)
            return _apply_outcome(outcome)

        # -------------------------------------------------------------
        # Supervisor liveness
        # -------------------------------------------------------------
        if not await _supervisor_is_alive(bus):
            log.error(
                "portfolio_start_rejected_no_supervisor",
                extra={"heartbeat_key": ROUTER_HEARTBEAT_KEY},
            )
            if reservation is not None:
                await idem.release(reservation.redis_key)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "live-supervisor is not running (no fresh router heartbeat). "
                    "Recreate the broker-profile supervisor with the current image: "
                    "`COMPOSE_PROFILES=broker docker compose up -d live-supervisor "
                    "ib-gateway` (use your environment's compose file, e.g. "
                    "docker-compose.prod.yml in prod, docker-compose.dev.yml in dev), "
                    "then retry."
                ),
            )

        # -------------------------------------------------------------
        # Cheap deploy-eligibility checks on a NON-LOCKING revision read
        # -------------------------------------------------------------
        # Council 2026-06-01 (bounded Option B): these checks (frozen-revision /
        # strategy-found / non-empty-members / not-archived-strategy) run BEFORE
        # STAGE-2's Key Vault read so a request that cannot deploy (e.g. a DRAFT
        # revision) returns its 400/404/422 without ever poking the credential
        # store. They read the revision WITHOUT ``FOR UPDATE``: a frozen revision
        # is immutable (the partial unique index allows at most one unfrozen row
        # per portfolio and freezing is one-way), so its values are stable without
        # a row lock — and acquiring a row lock HERE, then committing inside the
        # old STAGE-2 ``resolve_for_spawn``, was exactly the lock-release defect
        # this fork fixes. The upsert-critical revision ``FOR UPDATE`` (serializing
        # concurrent same-revision starts for the collision re-check) is acquired
        # LATER, in the final critical section AFTER credential validation, and
        # held through the upsert with no intervening commit.
        revision: LivePortfolioRevision | None = (
            await db.execute(
                select(LivePortfolioRevision).where(
                    LivePortfolioRevision.id == request.portfolio_revision_id
                )
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
        # SUPERVISOR path opts into the soft-delete filter (plan R20): an
        # active deployment that references a now-archived strategy must
        # still resolve its Strategy rows so status/positions endpoints
        # keep working until the deployment is stopped. But Codex iter-1
        # P1 flagged that POST /start-portfolio runs through this code
        # too — for NEW starts, an archived member must be REJECTED so
        # the soft-delete "removed from new operations" invariant holds.
        for strat_row in (
            (
                await db.execute(
                    select(Strategy)
                    .where(Strategy.id.in_(strategy_ids))
                    .execution_options(include_deleted=True)
                )
            )
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

        # Reject NEW starts whose members include archived strategies.
        # Active warm-restart paths reach this same code but won't trip
        # the check unless their members were archived in the meantime —
        # in which case rejecting is also correct (operator must
        # explicitly stop the deployment + re-snapshot with non-archived
        # members). The status-poll endpoints DO NOT enter this code path,
        # so existing running deployments keep resolving their Strategy
        # rows via the live-status / supervisor reads.
        archived = [str(s.id) for s in strategies_by_id.values() if s.deleted_at is not None]
        if archived:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Cannot start a deployment whose members include "
                    f"archived strategies: {archived}. Snapshot a new "
                    "revision without the archived members."
                ),
            )

        # -------------------------------------------------------------
        # Task 5: KV credential validation (STAGE 2 — credential read).
        # -------------------------------------------------------------
        # Council 2026-06-01 (bounded Option B): STAGE 2 reads the credential
        # store to PROVE resolvability but performs NO commit and holds NO DB row
        # lock — the KV read happens with no ``FOR UPDATE`` held, and the request
        # session's transaction is never released by a mid-handler commit (the
        # lock-invariant defect this fork fixes). It does NOT stamp
        # ``credentials_last_accessed`` — that is deferred to the final upsert
        # transaction below (atomic with the deployment row).
        #
        # Runs AFTER the idempotency reservation decided this request executes
        # (not a cached/in-flight replay) AND after the cheap deploy-eligibility
        # checks (frozen-revision / strategy-found / non-empty-members /
        # not-archived) above — so a request that cannot deploy (e.g. a DRAFT
        # revision) returns its 400/404/422 BEFORE any Key Vault read. The KV read
        # runs at most once per executed request (a replay is short-circuited by
        # the idempotency reservation). Only when an account was resolved (the
        # legacy warm-restart path has ``broker_account is None``). On failure,
        # fail closed; the cred reason was already counted inside
        # ``resolve_for_spawn`` (SPAWN_FAILED) so we don't double-count here. The
        # captured ``credentials_validated_version`` flows into STAGE 3's
        # rotation-version comparison and the upsert stamp below.
        credentials_validated_version: str | None = None
        if effective.broker_account is not None:
            broker_account_service = build_broker_account_service(http_request, db)
            cred_validation = await validate_account_credentials(
                effective.broker_account, broker_account_service
            )
            if not cred_validation.valid:
                # Always release the reservation before raising, in BOTH the
                # transient and permanent branches, so a retry is not stuck
                # in-flight (P2-A).
                if reservation is not None:
                    await idem.release(reservation.redis_key)
                if cred_validation.transient:
                    # TRANSIENT infra failure (Key Vault throttled/unreachable at
                    # deploy time) — retryable. Return 503 with a DISTINCT code so
                    # operators retry rather than reading a deploy-time KV outage
                    # as permanent invalid input (P2-A).
                    _deploy_error(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "BROKER_ACCOUNT_CREDENTIALS_UNAVAILABLE",
                        (
                            f"Broker account {effective_account_id} credential "
                            "store is temporarily unavailable "
                            f"({cred_validation.reason}); retry shortly."
                        ),
                        {
                            "account_id": effective_account_id,
                            "reason": cred_validation.reason,
                        },
                    )
                _deploy_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "BROKER_ACCOUNT_CREDENTIALS_INVALID",
                    (
                        f"Broker account {effective_account_id} credentials failed "
                        f"validation: {cred_validation.reason}."
                    ),
                    {
                        "account_id": effective_account_id,
                        "reason": cred_validation.reason,
                    },
                )
            credentials_validated_version = cred_validation.version

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
        # Task 5: identity recompute uses the EFFECTIVE account so the upsert's
        # ``identity_signature`` matches the warm-restart lookup done at the top
        # of the handler (and the binding resolver).
        identity = derive_portfolio_deployment_identity(
            user_id=user_id,
            portfolio_revision_id=request.portfolio_revision_id,
            account_id=effective_account_id,
            paper_trading=request.paper_trading,
            ib_login_key=effective_ib_login_key,
            user_sub=claims.get("sub"),
        )
        identity_signature = identity.signature()

        slug = generate_deployment_slug()
        now = datetime.now(UTC)
        deployment_table = LiveDeployment.__table__

        # =============================================================
        # FINAL CRITICAL SECTION — one transaction, NO commit until the
        # very end (council 2026-06-01, bounded Option B).
        # =============================================================
        # Lock ordering, established here AFTER STAGE-2 credential validation
        # (which read Key Vault with NO lock held and NO commit):
        #
        #   1. revision ``FOR UPDATE`` — serializes concurrent starts for the
        #      SAME portfolio revision (a superset of same-``(revision_id,
        #      account_id)`` starts), so the ``(revision_id, account_id)``
        #      collision re-check below runs UNDER a held lock and the loser
        #      returns 422 ``LIVE_DEPLOY_CONFLICT`` deterministically — never a
        #      raw ``IntegrityError`` 500. A frozen revision is immutable, so the
        #      lock is purely a serialization point, not a value-staleness guard.
        #   2. broker_accounts ``FOR UPDATE`` (STAGE 3, when an account was
        #      resolved) — serializes START vs ARCHIVE / credential ROTATION on
        #      THIS account's row.
        #
        # NO commit lies anywhere between these locks and the single ``db.commit()``
        # after the upsert below — the lock-release defect this fork fixes was
        # STAGE-2's ``resolve_for_spawn`` committing the shared request session
        # mid-handler, which silently dropped any ``FOR UPDATE`` lock held before
        # it. STAGE 2 no longer commits (``stamp_access=False``), so every lock
        # taken here survives to the upsert.
        await db.execute(
            select(LivePortfolioRevision.id)
            .where(LivePortfolioRevision.id == request.portfolio_revision_id)
            .with_for_update()
        )

        # -------------------------------------------------------------
        # STAGE 3: serialize START vs ARCHIVE / ROTATION on broker_accounts.
        # -------------------------------------------------------------
        # An operator could archive this account OR rotate its credentials AFTER
        # STAGE-2 validation (which holds no lock) but BEFORE the deployment row
        # below is inserted. Take a row-level ``FOR UPDATE`` lock on the
        # broker_accounts row and RE-ASSERT ACTIVE here, inside this SAME
        # transaction. Held until the upsert commits, this lock serializes us
        # against ``BrokerAccountService.archive``'s matching ``FOR UPDATE`` on the
        # same row: whoever commits first wins. If START commits first, archive
        # then sees our active deployment and is blocked (409); if archive commits
        # first, we re-read ARCHIVED here and fail closed BEFORE any insert/publish.
        # The locked re-read (``populate_existing``) also surfaces the FRESH
        # ``credentials_secret_version`` for the rotation guard. Only when an
        # account was resolved (legacy warm-restart has ``broker_account is None``
        # and is unaffected; its same-revision serialization is the revision lock
        # above).
        locked_broker_account: BrokerAccount | None = None
        if effective.broker_account is not None:
            active_recheck = await lock_and_assert_account_active(db, effective.broker_account.id)
            if not active_recheck.valid:
                if reservation is not None:
                    await idem.release(reservation.redis_key)
                DEPLOY_VALIDATION_FAILED.inc(
                    account_id=effective_account_id, reason=active_recheck.reason or "archived"
                )
                _deploy_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "DEPLOY_VALIDATION_FAILED",
                    (
                        f"Broker account {effective_account_id} was archived "
                        "before the deployment could be persisted. Re-activate "
                        "or select a different account, then retry."
                    ),
                    {
                        "account_id": effective_account_id,
                        "reason": active_recheck.reason or "archived",
                    },
                )

            # Credential-rotation race guard: STAGE 2 validated against
            # ``credentials_validated_version``; the STAGE-3 locked re-read just
            # surfaced the FRESH ``credentials_secret_version`` under the row lock.
            # If a rotation committed in the STAGE-2→STAGE-3 window the two DIFFER
            # — the deployment would otherwise be stamped/published as validated
            # against a now-stale secret version. Fail closed RETRYABLE (409): the
            # retry re-runs STAGE 2 against the new version. For ``legacy_env``
            # rows both are NULL (NULL == NULL → no false trip). Comparison only
            # when an account was resolved (legacy warm-restart has no version).
            if active_recheck.current_version != credentials_validated_version:
                if reservation is not None:
                    await idem.release(reservation.redis_key)
                DEPLOY_VALIDATION_FAILED.inc(
                    account_id=effective_account_id, reason="credentials_rotated"
                )
                _deploy_error(
                    status.HTTP_409_CONFLICT,
                    "BROKER_ACCOUNT_CREDENTIALS_ROTATED",
                    (
                        f"Broker account {effective_account_id} credentials were "
                        "rotated after validation but before the deployment could "
                        "be persisted. Retry — the retry re-validates against the "
                        "new credential version."
                    ),
                    {
                        "account_id": effective_account_id,
                        "reason": "credentials_rotated",
                    },
                )
            # The locked, freshly-populated ORM instance (same identity-map object
            # ``lock_and_assert_account_active`` re-read under the lock) — used
            # below to stamp ``credentials_last_accessed`` atomically with the
            # upsert (the deferred observability metadata STAGE 2 no longer stamps).
            locked_broker_account = await db.get(BrokerAccount, effective.broker_account.id)

        # --------------------------------------------------------------
        # UNIQUE(revision_id, account_id) collision re-check — UNDER LOCK.
        # --------------------------------------------------------------
        # Council 2026-06-01: this re-SELECT runs UNDER the held revision (and,
        # when resolved, broker_accounts) ``FOR UPDATE`` lock, immediately before
        # the upsert. Two concurrent same-``(revision_id, account_id)`` starts are
        # serialized by the revision lock above; the loser observes the winner's
        # committed row HERE and returns 422 ``LIVE_DEPLOY_CONFLICT`` rather than
        # surfacing a raw ``IntegrityError`` 500 from the
        # ``(revision_id, account_id)`` UNIQUE constraint. (Changing ib_login_key
        # produces a new identity_signature, but the row would still collide on
        # ``(revision_id, account_id)`` — operator must archive/stop the existing
        # row first.) See docs/plans/2026-05-13-live-deploy-safety-trio.md §Bug #1.
        existing_collision = (
            await db.execute(
                select(LiveDeployment).where(
                    LiveDeployment.portfolio_revision_id == request.portfolio_revision_id,
                    LiveDeployment.account_id == effective_account_id,
                    LiveDeployment.identity_signature != identity_signature,
                )
            )
        ).scalar_one_or_none()
        if existing_collision is not None:
            if reservation is not None:
                await idem.release(reservation.redis_key)
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
                            "requested_ib_login_key": effective_ib_login_key,
                            "requested_paper_trading": request.paper_trading,
                            "hint": (
                                "stop the existing deployment via POST /api/v1/live/stop, "
                                "then retry"
                            ),
                        },
                    }
                },
            )

        # Task 5: persist the EFFECTIVE account on the row + (when an account
        # was resolved) the broker-account linkage and credential-validation
        # stamps. ``broker_account_id`` / ``credentials_validated_*`` are NULL
        # on the legacy warm-restart path (no account resolved).
        broker_account_id = (
            effective.broker_account.id if effective.broker_account is not None else None
        )
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
            account_id=effective_account_id,
            ib_login_key=effective_ib_login_key,
            message_bus_stream=derive_message_bus_stream(slug),
            portfolio_revision_id=request.portfolio_revision_id,
            broker_account_id=broker_account_id,
            credentials_validated_at=(now if effective.broker_account is not None else None),
            credentials_validated_version=credentials_validated_version,
        )
        _active_statuses = ("starting", "building", "ready", "running")
        # On a warm restart the DO UPDATE SET must persist the freshly-resolved
        # broker linkage + credential-validation stamps — otherwise Postgres
        # keeps the insert's values out of the row and the link/stamps would be
        # silently discarded. Only overwrite them when an account was resolved
        # for THIS request (``broker_account is not None``); on the legacy
        # warm-restart path (no account resolved) leave the existing values
        # untouched so an established link is never nulled out.
        do_update_set: dict[str, Any] = {
            "status": case(
                (
                    deployment_table.c.status.in_(_active_statuses),
                    deployment_table.c.status,
                ),
                else_="starting",
            ),
            "last_started_at": now,
        }
        if effective.broker_account is not None:
            do_update_set["broker_account_id"] = stmt.excluded.broker_account_id
            do_update_set["credentials_validated_at"] = stmt.excluded.credentials_validated_at
            do_update_set["credentials_validated_version"] = (
                stmt.excluded.credentials_validated_version
            )
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=[deployment_table.c.identity_signature],
            set_=do_update_set,
        ).returning(deployment_table.c.id, deployment_table.c.deployment_slug)
        upsert_row = (await db.execute(upsert_stmt)).one()
        deployment_id = upsert_row.id
        is_warm_restart = upsert_row.deployment_slug != slug

        # Deferred credential-access stamp (council 2026-06-01, bounded Option B):
        # STAGE 2 no longer stamps ``credentials_last_accessed`` (it commits
        # nothing). Stamp it HERE — on the still-locked broker_accounts row, atomic
        # with the deployment upsert in this SAME transaction — so a SUCCESSFUL
        # deploy records the access while a deploy that FAILED CLOSED after STAGE 2
        # (archived 422 / rotation 409 / collision 422) leaves it UNCHANGED (those
        # branches raise before reaching here). It is pure observability metadata;
        # nothing branches on it, so deferring it costs nothing functionally.
        if locked_broker_account is not None:
            locked_broker_account.credentials_last_accessed = now

        await db.commit()

        # Set the secret-age gauge AFTER the successful commit (metrics are not
        # transactional). Skipped for ``legacy_env`` rows, which have no
        # ``credentials_updated_at``. Mirrors the gauge ``resolve_for_spawn`` used
        # to set on the data-plane (``stamp_access=True``) path.
        if (
            locked_broker_account is not None
            and locked_broker_account.credentials_updated_at is not None
        ):
            age_seconds = (now - locked_broker_account.credentials_updated_at).total_seconds()
            KV_SECRET_AGE.set(age_seconds, account_id=locked_broker_account.ib_account_id)

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
        # Graduation linking — link the pre-resolved candidates and
        # transition stage (Bug #3 plan §step 4, Codex round-1 P1 fix:
        # MUST happen BEFORE publish_start so a START with no linked
        # candidate is never on the bus). Re-queries each candidate
        # with SELECT FOR UPDATE + re-checks binding (Codex round-1 P1
        # fix: pre-resolved candidate can race with a concurrent
        # archive / config edit / another first-deploy; the FOR UPDATE
        # serializes us against that and the binding re-check catches
        # any drift since pre-reserve).
        # -------------------------------------------------------------
        from msai.models.graduation_candidate import GraduationCandidate
        from msai.services.graduation import (
            ELIGIBLE_FOR_LIVE_PORTFOLIO,
            GraduationService,
        )
        from msai.services.live.snapshot_binding import (
            BindingInstrumentsMissingError,
            BindingMismatchError,
            candidate_instruments,
            verify_member_matches_candidate,
        )
        from msai.services.nautilus.live_instrument_bootstrap import (
            exchange_local_today,
        )
        from msai.services.nautilus.security_master.live_resolver import (
            LiveResolverError,
            lookup_for_live,
        )

        # Codex round-3 P2 (widens round-2 P1#2): the supervisor's
        # startup watchdog scans `live_node_processes`, NOT
        # `live_deployments`. Pre-publish there is no process row, so
        # ANY raise in the link loop OR commit+publish leaves the
        # deployment row stuck in `starting` until operator intervention.
        # Wrap the FULL link block + commit + publish in one try/except;
        # on any failure flip deployment.status="failed" so the next
        # retry's warm-restart upsert reactivates it cleanly.
        graduation_service = GraduationService()
        try:
            for (member, pre_resolved_candidate), (_m_canon, _c_canon) in zip(
                resolved_bindings, canonical_per_member, strict=True
            ):
                locked = (
                    await db.execute(
                        select(GraduationCandidate)
                        .where(GraduationCandidate.id == pre_resolved_candidate.id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if locked is None:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "error": {
                                "code": "LIVE_DEPLOY_REPAIR_REQUIRED",
                                "message": (
                                    f"Candidate {pre_resolved_candidate.id} was deleted "
                                    f"between binding pre-reserve and stage-link. "
                                    f"Operator must restore it or archive the deployment row."
                                ),
                            }
                        },
                    )
                if locked.stage not in ELIGIBLE_FOR_LIVE_PORTFOLIO:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "error": {
                                "code": "BINDING_INELIGIBLE",
                                "message": (
                                    f"Candidate {locked.id} drifted to stage `{locked.stage}` "
                                    f"between binding pre-reserve and stage-link."
                                ),
                                "details": {
                                    "candidate_id": str(locked.id),
                                    "stage": locked.stage,
                                },
                            }
                        },
                    )
                # Re-verify binding against the LOCKED row — content could
                # have changed via a concurrent operator edit. Codex round-2
                # P1#1: re-canonicalize the candidate's instruments from
                # the LOCKED row. Codex round-3 P2: also re-canonicalize
                # the MEMBER side at the SAME as_of so day-boundary alias
                # drift doesn't false-reject equivalent symbols.
                try:
                    locked_raw_inst = candidate_instruments(locked)
                except BindingInstrumentsMissingError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "error": {
                                "code": "BINDING_INSTRUMENTS_MISSING",
                                "message": str(exc),
                                "details": {"candidate_id": str(locked.id)},
                            }
                        },
                    ) from exc
                link_as_of = exchange_local_today()
                try:
                    locked_canon = [
                        r.canonical_id
                        for r in await lookup_for_live(
                            locked_raw_inst, as_of_date=link_as_of, session=db
                        )
                    ]
                    link_member_canon = [
                        r.canonical_id
                        for r in await lookup_for_live(
                            list(member.instruments), as_of_date=link_as_of, session=db
                        )
                    ]
                except LiveResolverError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "error": {
                                "code": "BINDING_INSTRUMENT_RESOLVE_FAILED",
                                "message": str(exc),
                                "details": {"candidate_id": str(locked.id)},
                            }
                        },
                    ) from exc
                try:
                    verify_member_matches_candidate(
                        member,
                        locked,
                        member_instruments_canonical=link_member_canon,
                        candidate_instruments_canonical=locked_canon,
                    )
                except BindingMismatchError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "error": {
                                "code": "BINDING_MISMATCH",
                                "message": str(exc),
                                "details": {
                                    "member_id": str(member.id),
                                    "candidate_id": str(locked.id),
                                    "mismatches": exc.mismatches,
                                    "hint": ("candidate drifted between pre-reserve and link"),
                                },
                            }
                        },
                    ) from exc
                except BindingInstrumentsMissingError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "error": {
                                "code": "BINDING_INSTRUMENTS_MISSING",
                                "message": str(exc),
                            }
                        },
                    ) from exc

                # Codex round-3 P1: guard against concurrent starts that
                # already linked this candidate to a different deployment.
                # Two cold starts of the same revision against two accounts
                # both pre-reserve while `deployment_id` is NULL; whichever
                # holds the FOR UPDATE lock second would otherwise overwrite
                # the first deploy's FK and orphan its audit link, breaking
                # warm restarts of the first deploy (LIVE_DEPLOY_REPAIR_REQUIRED).
                if locked.deployment_id is not None and locked.deployment_id != deployment.id:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "error": {
                                "code": "BINDING_AMBIGUOUS",
                                "message": (
                                    f"Candidate {locked.id} is already linked to a "
                                    f"different deployment ({locked.deployment_id}). "
                                    "Stop or archive that deployment, or re-graduate "
                                    "a fresh candidate for this strategy."
                                ),
                                "details": {
                                    "candidate_id": str(locked.id),
                                    "linked_deployment_id": str(locked.deployment_id),
                                    "this_deployment_id": str(deployment.id),
                                },
                            }
                        },
                    )
                locked.deployment_id = deployment.id
                # Stage transition rules (plan §Bug #3 step 4):
                #   live_candidate → live_running for first deploy
                #   paused         → live_running for resume
                #   live_running   → live_running no-op for restart
                if locked.stage == "live_candidate":
                    await graduation_service.update_stage(
                        db,
                        locked.id,
                        new_stage="live_running",
                        reason="live_deploy",
                        user_id=user_id,
                    )
                elif locked.stage == "paused":
                    await graduation_service.update_stage(
                        db,
                        locked.id,
                        new_stage="live_running",
                        reason="live_resume",
                        user_id=user_id,
                    )
                # else: already at live_running (warm restart) — no transition.
                log.info(
                    "graduation_candidate_linked",
                    extra={
                        "candidate_id": str(locked.id),
                        "deployment_id": str(deployment.id),
                        "strategy_id": str(member.strategy_id),
                        "stage_after": locked.stage,
                    },
                )
            await db.commit()
            await bus.publish_start(
                deployment_id=deployment.id,
                payload={
                    "deployment_slug": deployment.deployment_slug,
                    "strategy_id": str(first_strategy.id),
                    "strategy_path": first_strategy.file_path,
                    "config": combined_config,
                    "instruments": all_instruments,
                    # PR 1 T6 + council 2026-05-27 obj #2 / 2026-05-29 obj #12:
                    # carry the gateway-session key end-to-end so the
                    # supervisor's per-session startup serialization isn't
                    # silently degraded to global.
                    "gateway_session_key": deployment.ib_login_key,
                },
                idempotency_key=idempotency_key,
                # PR 2 T4: route the START onto the per-account command
                # stream so the per-account supervisor consumer picks it up.
                account_id=deployment.account_id,
            )
        except Exception:
            log.exception(
                "live_start_portfolio_link_or_publish_failed",
                extra={"deployment_id": str(deployment.id)},
            )
            # Best-effort: flip the deployment row to `failed` so it
            # doesn't dangle in `starting`. Any stage transition the
            # link loop already performed is left in place — operator's
            # retry is a warm-restart, which re-verifies binding against
            # the same candidate and re-runs the link block (which is a
            # no-op for already-linked candidates that pass binding).
            try:
                await db.rollback()
                deployment.status = "failed"
                deployment.last_stopped_at = datetime.now(UTC)
                await db.commit()
            except Exception:  # noqa: BLE001
                log.exception("deployment_status_failed_mark_failed")
            raise

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
        except Exception as exc:  # noqa: BLE001
            # iter-4 SF P3: DEBUG was invisible in production. A real
            # registration failure here leaves the deployment's events
            # unreachable to the projection consumer (status/positions
            # stream stops flowing) — operator should see this.
            log.warning(
                "stream_registry_register_failed",
                deployment_id=str(deployment.id),
                stream_name=deployment.message_bus_stream,
                error=str(exc),
                error_type=type(exc).__name__,
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
            # Capture the PK as a plain value NOW, while ``deployment`` is still a
            # live persistent instance. The ``db.rollback()`` below expires every
            # attribute on the session's objects, so any later ``deployment.id``
            # access triggers a lazy refresh — illegal in this sync context (no
            # greenlet) and the source of a ``MissingGreenlet`` 500 that masked the
            # honest 504 on the poll-timeout path (E2E 2026-06-01).
            deployment_id = deployment.id
            # PR 2 T4 review P1 — stranded-START flip guard (API side).
            #
            # The poll timed out (60s). TWO very different states look the
            # same from here:
            #
            #   (a) The START genuinely STRANDED — no per-account consumer
            #       attached, nothing was spawned, no live_node_processes row
            #       exists. Safe to flip the deployment to ``failed`` so a
            #       later XAUTOCLAIM re-delivery of this same START is
            #       recognised as STALE by the supervisor and dropped — it
            #       must NOT silently resurrect a node the operator (who just
            #       got a 504) walked away from.
            #
            #   (b) The node is SLOW to build/reconcile — a per-account
            #       consumer DID spawn it and its live_node_processes row is
            #       in ``starting`` / ``building`` (IB connect + reconciliation
            #       can run well past the 60s poll; the supervisor watchdog
            #       tolerates ``startup_hard_timeout_s``=1800s). This is a LIVE
            #       real-money node. Flipping the deployment to ``failed`` here
            #       would orphan it: invisible to ``/live/status?active_only``
            #       AND to the deploy gate, so a routine deploy could recreate
            #       the supervisor while a real-money node is unaccounted-for.
            #
            # So flip ONLY in case (a). In case (b) we leave the deployment in
            # its non-terminal status (``starting``/``building``) — fail-CLOSED:
            # it stays gated and visible; the supervisor's startup watchdog is
            # the authority that eventually flips a genuinely-wedged build.
            # The operator's 504 is honest ("still building"), not a lie that
            # hides a live node. Best-effort: a failure to read/mark here
            # leaves the row in ``starting``; the watchdog is the backstop.
            try:
                await db.rollback()
                if await _active_node_process_exists(db, deployment_id):
                    log.warning(
                        "start_portfolio_poll_timeout_node_still_building",
                        deployment_id=str(deployment_id),
                    )
                else:
                    fresh = await db.get(LiveDeployment, deployment_id)
                    if fresh is not None and fresh.status in ("starting", "building", "ready"):
                        fresh.status = "failed"
                        fresh.last_stopped_at = datetime.now(UTC)
                        await db.commit()
            except Exception:  # noqa: BLE001
                log.warning(
                    "start_portfolio_poll_timeout_mark_failed_failed",
                    deployment_id=str(deployment_id),
                )
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
                    "account_id": effective_account_id,
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

        if kind is FailureKind.ACCOUNT_HALT_ACTIVE:
            # Codex iter 4 P2-1: a START that races with /drain/{account_id}
            # and trips the supervisor's post-payload-factory account-halt
            # re-check lands here. Without this special case, the kind would
            # fall through to UNKNOWN and an unrelated 503 would be cached
            # under the caller's Idempotency-Key. Task 5: use the EFFECTIVE
            # account (the one actually published), not the raw request.
            outcome = EndpointOutcome.account_halt_active(effective_account_id)
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
    # Council 2026-06-01 (LOCK-ORDER MIGRATION — deployment-FIRST): take the
    # ``live_deployments`` row lock ``FOR UPDATE`` FIRST — BEFORE any
    # ``live_node_processes`` ``FOR UPDATE`` below — matching the global
    # supervisor invariant ``advisory(gateway) → live_deployments FOR UPDATE →
    # live_node_processes FOR UPDATE``. This serialises the operator /stop
    # intent-stamp against the supervisor's Phase-A slot reservation (which also
    # locks this deployment row first): if /stop wins the lock, Phase-A then sees
    # the durable ``stop_requested_at`` (OPERATOR_STOPPED suppress); if Phase-A
    # wins, /stop blocks then sees the reserved active row and stamps it. The
    # lock is held until the first ``db.commit()`` below (which covers BOTH the
    # no-active-row failed-pending stamp AND the active-row stamp branch), so the
    # node-row ``FOR UPDATE``s in those branches are always taken AFTER the
    # deployment lock — no node→deployment edge.
    result = await db.execute(
        select(LiveDeployment).where(LiveDeployment.id == request.deployment_id).with_for_update()
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
        # FINDING 1 (P1) — suppress a PENDING / FUTURE auto-restart on a FAILED
        # latest row. A plain /stop sets NO halt latch, so when the deployment's
        # latest row has already been classified ``failed`` and is restart-
        # ELIGIBLE, the active-row stamping below never runs (there is no active
        # row) — and without a durable stop intent on that failed row a restart
        # would respawn the very node the operator just stopped. Stamp
        # ``stop_requested_at`` on that latest failed row HERE (under ``FOR
        # UPDATE``) so BOTH restart paths honor it: the reaper-driven
        # ``_attempt_auto_restart`` (pre/post-backoff ``_operator_stop_requested``
        # re-check + the atomic Phase-A re-check) AND the periodic
        # ``rescan_for_restart`` (whose candidate query gates
        # ``stop_requested_at IS NULL``).
        #
        # FIX 1 (Codex P1 #1) — the prior code required ``restart_dispatched_at
        # IS NOT NULL`` (a reaper-dispatched sentinel). That MISSED two restart-
        # eligible shapes:
        #   (a) a supervisor-outage-window crash the PERIODIC RESCAN will pick up
        #       — the rescan restarts ``failed`` deployments and does NOT require
        #       ``restart_dispatched_at``; and
        #   (b) a restart task that GAVE UP and cleared the sentinel
        #       (``restart_dispatched_at`` back to NULL) but whose deployment is
        #       still ``failed`` and rescan-eligible.
        # In both, the prior filter SKIPPED stamping → /stop returned "stopped" →
        # the rescan later saw ``stop_requested_at IS NULL`` → auto-restarted the
        # stopped deployment. Broaden to: stamp the LATEST row whenever it is
        # ``failed`` AND ``stop_requested_at IS NULL`` (drop the
        # ``restart_dispatched_at`` requirement). Stamping intent on a failed
        # latest row that turns out NOT to be restart-eligible (e.g. a permanent
        # SPAWN_FAILED kind the rescan excludes) is HARMLESS — the intent column
        # is advisory and the row is never resurrected. We read the LATEST row by
        # ``started_at`` (the SAME row the rescan + reaper classify on) so we
        # stamp exactly the row those paths gate on.
        #
        # We touch ONLY the durable intent column — never the row ``status`` or
        # ``LiveDeployment.status`` — so the failed row is NOT resurrected /
        # re-activated; the intent just becomes visible to the restart paths (and
        # survives a supervisor restart). Skipped when the deployment is already
        # terminally ``stopped`` (nothing pending to suppress).
        if deployment.status != "stopped":
            latest_row = (
                await db.execute(
                    select(LiveNodeProcess)
                    .where(LiveNodeProcess.deployment_id == deployment.id)
                    .order_by(LiveNodeProcess.started_at.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            failed_pending = (
                latest_row
                if latest_row is not None
                and latest_row.status == "failed"
                and latest_row.stop_requested_at is None
                else None
            )
            if failed_pending is not None:
                failed_pending.stop_requested_at = datetime.now(UTC)
                await db.commit()
                log.info(
                    "live_stop_suppressed_pending_restart",
                    deployment_id=str(deployment.id),
                    failed_row_id=str(failed_pending.id),
                )
                # The durable ``stop_requested_at`` stamped above is the
                # AUTHORITATIVE suppressor: the in-flight ``_run_restart_task``
                # re-reads it (``_operator_stop_requested``) under ``FOR UPDATE``
                # before every respawn — and after the backoff — and aborts
                # (SUPPRESSED) when set. The intent survives a supervisor restart
                # (it is on the DB row, not in supervisor memory), so the
                # startup re-scan honors it too. There is no active row to signal
                # here, so we do NOT publish a STOP command (the SIGTERM path is
                # for live nodes). Immediate task cancellation happens on the
                # regular /stop path (``FleetRouter.stop`` → ``cancel_restart_
                # task``) where a STOP command IS consumed; in this dead-node
                # branch the durable intent is sufficient and the task aborts at
                # its next respawn re-check.
            elif deployment.status in ("starting", "building", "ready", "running"):
                # INVARIANT 1 (council 2026-06-01 follow-up — QUEUED-START GAP).
                # No active node row AND no pending-failed row to stamp, but the
                # deployment is still in a NON-TERMINAL PRE-ACTIVE state — a START
                # was PUBLISHED but NOT yet consumed (Phase A never reserved a node
                # row), OR the latest node row is a prior ``stopped``/non-failed row
                # from an earlier lifecycle. There is no node row to carry
                # ``stop_requested_at``, so we mark the ``LiveDeployment`` row
                # itself operator-terminal (``status='stopped'``) under THIS ``FOR
                # UPDATE`` lock. The supervisor's Phase-A OPERATOR-TERMINAL
                # DEPLOYMENT GATE then aborts the queued START (initial + any
                # redelivery + any restart_carry respawn) — never spawning a live
                # node for an account the operator already stopped. Mirrors
                # ``FleetRouter.stop``'s pre-active no-active-row branch.
                deployment.status = "stopped"
                deployment.last_stopped_at = datetime.now(UTC)
                await db.commit()
                log.info(
                    "live_stop_marked_queued_start_deployment_stopped",
                    deployment_id=str(deployment.id),
                )

        # PR #65 Codex P2 round-4: if a prior /stop returned
        # FLATNESS_UNKNOWN, persist that state so a retry doesn't
        # silently 200 here. The marker key `stop_unknown:{deployment_id}`
        # lives for 1h (long enough for an operator IB-portal check
        # + manual flatten + /resume, short enough not to dirty Redis).
        # Operator can clear it explicitly via /resume (Layer 1) which
        # is the same path that clears the halt flag.
        with contextlib.suppress(Exception):
            unknown_marker = await bus._redis.get(  # noqa: SLF001
                f"stop_unknown:{deployment.id}"
            )
            if unknown_marker:
                # Marker carries the original stop_nonce for traceability.
                return _apply_outcome(
                    EndpointOutcome.flatness_unknown(
                        deployment_id=str(deployment.id),
                        stop_nonce=str(unknown_marker),
                        process_status="stopped",
                    )
                )
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

    # Durable operator-stop intent (PR 2 T6 / council #3 F5) — set
    # SYNCHRONOUSLY at the API layer, committed under a row lock, BEFORE the
    # STOP command is published.
    #
    # REAL-MONEY P1 (adversarial-safety-review): unlike /kill-all and /drain
    # (which set their halt LATCH synchronously here so the reaper's
    # fail-closed halt gate backstops the publish→consume gap), a plain /stop
    # sets NO halt latch. So without this synchronous stamp there is a window:
    # the API publishes STOP, the supervisor has not yet CONSUMED it (only
    # ``FleetRouter.stop()`` — which runs on consume — used to set
    # ``stop_requested_at``), and the node self-crashes (non-zero exit) in
    # that gap. The reaper's ``_on_child_exit`` would then see
    # ``stop_requested_at IS NULL`` → classify it as a non-operator-stop
    # failure → AUTO-RESTART the very node the operator just stopped, letting
    # a resurrected node submit fresh orders for a stopped account until the
    # pending STOP is finally consumed. Stamping the durable intent here
    # closes that gap: the reaper (which classifies under its own ``FOR
    # UPDATE``) either reads this committed value or blocks until this commits.
    #
    # Mirrors the kill-all/drain in-handler synchronous-marker placement. The
    # supervisor-side ``FleetRouter.stop()`` re-stamps ``stop_requested_at``
    # idempotently on consume; that is harmless (it only re-writes the same
    # intent). Scoped to the CURRENTLY-ACTIVE node rows for this deployment so
    # a terminal/historical row is left untouched. We do NOT touch
    # ``LiveDeployment.status`` here — only the durable per-row stop intent —
    # so /resume / re-deploy semantics are unaffected (unlike /drain, which
    # owns the account halt latch).
    stop_intent_at = datetime.now(UTC)
    active_rows_for_intent = (
        (
            await db.execute(
                select(LiveNodeProcess)
                .where(
                    LiveNodeProcess.deployment_id == deployment.id,
                    LiveNodeProcess.status.in_(
                        ("starting", "building", "ready", "running", "stopping")
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for active_row in active_rows_for_intent:
        active_row.stop_requested_at = stop_intent_at
    await db.commit()

    # Publish STOP_AND_REPORT_FLATNESS via SET-NX coalescing — concurrent
    # /stop callers converge on the originator's nonce (Codex iter-6 P2 #1).
    stop_nonce, _is_originator = await coalesce_or_publish_stop_with_flatness(
        redis=bus._redis,  # noqa: SLF001 — intentional bus.redis reuse
        bus=bus,
        deployment_id=deployment.id,
        member_strategy_id_fulls=member_strategy_id_fulls,
        reason="user",
        idempotency_key=idempotency_key,
        # PR 2 T4: STOP routes onto the deployment's per-account stream.
        account_id=deployment.account_id,
    )

    # Poll for the child's STOP_REPORT (30 s deadline). Does NOT DEL —
    # 120 s TTL handles cleanup; coalesced readers share the key.
    report = await poll_stop_report(
        redis=bus._redis,  # noqa: SLF001
        stop_nonce=stop_nonce,
        # 45s deadline — clears the 30s XAUTOCLAIM idle window on the
        # command bus so a cross-host redelivery (Phase 2 multi-supervisor
        # topology) or a restart-during-stop scenario can still produce
        # the report before this caller times out. Single-supervisor
        # topology (Phase 1) doesn't actually need the headroom, but
        # the cost is bounded (caller waits up to 45s only on the
        # genuinely-degraded path; healthy stops resolve in ~10s).
        # PR #65 Codex P2 round-6.
        deadline_s=45.0,
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

    # PR #65 Codex P1 round-3: row is terminal but no flatness report
    # arrived. The supervisor closed out, but the wire that verifies
    # broker positions never confirmed. Surface as 504
    # FLATNESS_UNKNOWN per the runbook — refusing to silently report
    # `broker_flat: null` as success.
    if report is None:
        log.warning(
            "live_deployment_stopped_flatness_unknown",
            deployment_id=str(deployment.id),
            stop_nonce=stop_nonce,
            process_status=row.status,
        )
        # Persist the unknown state so a retry doesn't hit the
        # "no active process → 200 stopped" shortcut at the top of
        # this endpoint and silently swallow the warning (PR #65
        # Codex P2 round-4). 1h TTL; cleared by /resume.
        with contextlib.suppress(Exception):
            await bus._redis.set(  # noqa: SLF001
                f"stop_unknown:{deployment.id}",
                stop_nonce,
                ex=3600,
            )
        return _apply_outcome(
            EndpointOutcome.flatness_unknown(
                deployment_id=str(deployment.id),
                stop_nonce=stop_nonce,
                process_status=row.status,
            )
        )

    # Past here `report` is guaranteed non-None (early returns above).
    await log_audit(
        db,
        user_id=deployment.started_by,
        action="live_stop",
        resource_type="live_deployment",
        resource_id=deployment.id,
        details={
            "stop_nonce": stop_nonce,
            "broker_flat": report["broker_flat"],
        },
    )

    log.info(
        "live_deployment_stopped",
        deployment_id=str(deployment.id),
        process_status=row.status,
        broker_flat=report["broker_flat"],
        stop_nonce=stop_nonce,
    )

    return _apply_outcome(
        EndpointOutcome.stopped(
            {
                "id": str(deployment.id),
                "status": "stopped",
                "process_status": row.status,
                "stop_nonce": stop_nonce,
                "broker_flat": report["broker_flat"],
                "remaining_positions": report["remaining_positions"],
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
    FleetRouter.spawn). The supervisor re-checks the halt
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
    #
    # PR 1 T3 + decision-doc addendum 2026-05-28 halt-cause schema:
    # write a structured cause-attribution companion key so operators
    # (and PR 1b's data-stale auto-halt) can distinguish a manual
    # /kill-all from an automated halt.
    #
    # F9 fix (Codex iter 2 P1 / silent-failure-hunter F3): all halt-set
    # writes are atomic (all-or-nothing) so an emergency-stop endpoint
    # cannot leave Redis in a partial state. The prior implementation
    # issued 5 sequential ``await SET`` calls; a connection drop
    # mid-sequence would set the latch but skip the cause-companion (or
    # vice-versa), leaving operators with a half-finished kill switch.
    #
    # PR 1b T6: switched from a MULTI/EXEC pipeline that UNCONDITIONALLY
    # SET the cause key to the shared ``HALT_WRITE_LUA`` (one atomic
    # round-trip). The pipeline's blind cause SET silently ERASED a
    # data-stale auto-halt's attribution if the operator hit /kill-all
    # while a data-stale halt was already latched. ``HALT_WRITE_LUA``
    # writes the cause ONLY-IF-ABSENT (``SET ... NX``) so a pre-existing
    # auto-halt cause is PRESERVED, and ALWAYS appends this manual cause
    # onto the capped ``:history`` list so the kill-all is still recorded.
    # Any RedisError propagates as a 5xx so the operator sees the failure
    # instead of a green response.
    cause_payload = {
        "reason": HaltCause.FLEET_EMERGENCY.value,
        "detected_at": halt_set_at.isoformat(),
        "source": str(user_id) if user_id else "operator",
    }
    _halt_keys, _halt_argv = fleet_halt_write_args(
        set_by=str(user_id) if user_id else "unknown",
        set_at=halt_set_at.isoformat(),
        cause_json=json.dumps(cause_payload),
        ttl_s=HALT_TTL_SECONDS,
    )
    await bus._redis.eval(HALT_WRITE_LUA, len(_halt_keys), *_halt_keys, *_halt_argv)  # type: ignore[misc]  # noqa: SLF001

    # Layer 3: query active live_node_processes rows and
    # publish a stop command for each. Use the explicit
    # status set so a row in a terminal state ('stopped',
    # 'failed') doesn't get a useless stop command.
    #
    # PR 2 T4: join to ``live_deployments`` to recover each process's
    # ``account_id`` so the STOP is published onto that account's
    # per-account command stream (``live_node_processes`` itself carries
    # no account_id; the deployment row does).
    active_statuses = ("starting", "building", "ready", "running")
    rows = (
        await db.execute(
            select(LiveNodeProcess, LiveDeployment.account_id)
            .join(LiveDeployment, LiveDeployment.id == LiveNodeProcess.deployment_id)
            .where(LiveNodeProcess.status.in_(active_statuses))
        )
    ).all()

    stopped = 0
    failed: list[str] = []
    flatness_nonces: dict[str, str] = {}  # deployment_id -> stop_nonce
    for row, row_account_id in rows:
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
                # PR 2 T4: route STOP onto this process's account stream.
                account_id=row_account_id,
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
                # 35s deadline — clears the 30s XAUTOCLAIM idle window
                # plus a 5s buffer. Tighter than /stop's 45s because
                # the panic-button caller benefits from a faster answer
                # even at the cost of more `broker_flat: null` reports
                # on cross-host redelivery races. Operator already
                # knows kill-all requires IB-portal verification (per
                # the ADR runbook) so an early `null` is recoverable.
                # PR #65 Codex P2 round-6.
                deadline_s=35.0,
            )

        results = await asyncio.gather(*(_poll_one(d, n) for d, n in flatness_nonces.items()))
        flatness_results = dict(results)

        # PR #65 Codex P2 round-3: mirror the /stop cleanup. Clear
        # `inflight_stop:{deployment_id}` for every deployment whose
        # report arrived, so an operator who resumes + warm-restarts
        # + re-stops within the 60s TTL doesn't have the next /stop
        # call coalesce onto this kill-all's already-completed nonce.
        # Best-effort: 60s TTL is the fallback if DEL fails.
        for dep_id, report in flatness_results.items():
            if report is not None:
                with contextlib.suppress(Exception):
                    await bus._redis.delete(f"inflight_stop:{dep_id}")  # noqa: SLF001

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
                any_non_flat=any_non_flat,
                flatness_reports=flatness_summary,
            ).model_dump(),
        )

    log.critical(
        "kill_all_executed",
        stopped=stopped,
        any_non_flat=any_non_flat,
    )
    if any_non_flat:
        # PR #65 Codex P2: surface non-flat outcome in the HTTP layer,
        # not just audit. The panic-button caller MUST see this
        # without grepping the audit log. 207 Multi-Status — kill-all
        # itself succeeded (all SIGTERMs sent), but at least one
        # deployment has positions still on the broker requiring
        # manual flatten via IB portal before /resume.
        return JSONResponse(
            status_code=207,
            content=LiveKillAllResponse(
                stopped=stopped,
                failed_publish=0,
                risk_halted=True,
                any_non_flat=True,
                flatness_reports=flatness_summary,
            ).model_dump(),
        )
    return LiveKillAllResponse(
        stopped=stopped,
        failed_publish=0,
        risk_halted=True,
        any_non_flat=False,
        flatness_reports=flatness_summary,
    )


def _normalize_account_id_path_param(account_id: str) -> str:
    """Strip leading/trailing whitespace + reject internal whitespace
    or empty values on path-param account ids.

    Mirror of ``PortfolioStartRequest._normalize_account_id`` (schema
    layer). Both produce the SAME final string so the halt-latch key
    written here and the supervisor's per-account halt-check key are
    byte-identical. Without this, percent-encoded path whitespace like
    ``/api/v1/live/drain/%20DUP733214%20`` slips through and the
    supervisor reads a different Redis key than was written. Codex
    iter 17 P2.
    """
    normalized = account_id.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail="account_id is required")
    if any(ch.isspace() for ch in normalized):
        raise HTTPException(
            status_code=422,
            detail=(
                "account_id contains whitespace — IB account ids are "
                "alphanumeric (e.g. 'DUP733214' or 'U1234567')"
            ),
        )
    return normalized


@router.post("/drain/{account_id}", response_model=None)
async def live_drain_account(
    account_id: str,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
) -> dict[str, Any] | JSONResponse:
    """Account-scoped drain (PR 1 T8).

    Per council 2026-05-29 obj #11: keyed by ``account_id`` (NOT
    ``ib_login_key``) so two sub-accounts under the same TWS login have
    independent halt latches. Draining ``DUP733214`` does NOT touch
    ``DUP733215``.

    Distinct from ``/kill-all``: scoped to one account, no fleet-wide
    halt latch, halt-cause is ``HaltCause.OPERATOR_DRAIN``. Other
    accounts under the same login keep running.
    """
    account_id = _normalize_account_id_path_param(account_id)

    halt_set_at = datetime.now(UTC)
    user_id = await _resolve_user_id(db, claims)

    # Account-scoped halt latch (NOT the fleet key). Idempotent: setting
    # it again on a re-drain is harmless (TTL refreshed).
    #
    # Codex iter 5 P2-2: batch the latch + cause-companion writes through a
    # Redis transactional pipeline so a mid-sequence Redis error can't leave
    # the latch active without cause metadata (mirrors the /kill-all
    # transactional pipeline added in iter 2 F9).
    cause_payload = {
        "reason": HaltCause.OPERATOR_DRAIN.value,
        "detected_at": halt_set_at.isoformat(),
        "source": str(user_id) if user_id else "operator",
        "account_id": account_id,
    }
    async with bus._redis.pipeline(transaction=True) as _drain_pipe:  # noqa: SLF001
        _drain_pipe.set(account_halt_key(account_id), "true", ex=86400)
        _drain_pipe.set(
            halt_cause_key("account", account_id=account_id),
            json.dumps(cause_payload),
            ex=86400,
        )
        await _drain_pipe.execute()

    # Find active deployments matching account_id and publish STOP to each.
    active_statuses = ("starting", "building", "ready", "running")
    rows = (
        (
            await db.execute(
                select(LiveDeployment).where(
                    LiveDeployment.account_id == account_id,
                    LiveDeployment.status.in_(active_statuses),
                )
            )
        )
        .scalars()
        .all()
    )
    # F4 fix (silent-failure-hunter F1) + Codex iter 14 P2 (PR 1):
    # the drain STOP-publish loop must NOT swallow failures behind a
    # 200 response AND must NOT report drain success until the
    # supervisor has confirmed each deployment is stopped + flat.
    # Mirror the /kill-all flatness-polling pattern: publish STOP
    # with a flatness nonce, then parallel-poll ``stop_report:{nonce}``.
    # Without this, ``/drain/{account_id}`` returns 200 after merely
    # writing STOP commands to Redis — the TradingNode may still be
    # running and positions may still be open.
    import redis as _redis_pkg  # narrow exception clause (Codex-safe)

    stopped: list[str] = []
    failed: list[dict[str, str]] = []
    flatness_nonces: dict[str, str] = {}  # deployment_id -> stop_nonce
    for dep in rows:
        try:
            # Gather member strategy_id_fulls so the child reports
            # deployment-scoped flatness (Bug #2 pattern from /kill-all).
            member_rows = (
                await db.execute(
                    select(LiveDeploymentStrategy.strategy_id_full).where(
                        LiveDeploymentStrategy.deployment_id == dep.id
                    )
                )
            ).all()
            members = [r.strategy_id_full for r in member_rows]
            nonce, _ = await coalesce_or_publish_stop_with_flatness(
                redis=bus._redis,  # noqa: SLF001
                bus=bus,
                deployment_id=dep.id,
                member_strategy_id_fulls=members,
                reason="operator_drain",
                # PR 2 T4: route STOP onto this account's per-account stream.
                account_id=dep.account_id,
            )
            flatness_nonces[str(dep.id)] = nonce
            stopped.append(str(dep.id))
        except (_redis_pkg.RedisError, ConnectionError, TimeoutError) as exc:
            # Narrow exception clause: redis-py exceptions (RedisError covers
            # ConnectionError/ResponseError/TimeoutError from redis>=4.6) +
            # the asyncio TimeoutError surface. Anything else propagates as
            # a 500 — that's an unexpected bug, not a "partial-stop"
            # operator-actionable failure mode.
            log.exception(
                "drain_publish_stop_failed",
                extra={"deployment_id": str(dep.id), "account_id": account_id},
            )
            failed.append({"deployment_id": str(dep.id), "error": str(exc)})

    # Parallel-poll flatness reports for every deployment we successfully
    # published STOP for. 35s deadline matches /kill-all (XAUTOCLAIM idle
    # window + 5s buffer). If the supervisor doesn't write a report in
    # that window we surface ``broker_flat=None`` so the operator sees
    # the unknown-state truth instead of a misleading 200.
    flatness_results: dict[str, dict[str, Any] | None] = {}
    if flatness_nonces:

        async def _poll_one(dep_id: str, nce: str) -> tuple[str, dict[str, Any] | None]:
            return dep_id, await poll_stop_report(
                redis=bus._redis,  # noqa: SLF001
                stop_nonce=nce,
                deadline_s=35.0,
            )

        results = await asyncio.gather(*(_poll_one(d, n) for d, n in flatness_nonces.items()))
        flatness_results = dict(results)
        for dep_id, report in flatness_results.items():
            if report is not None:
                with contextlib.suppress(Exception):
                    await bus._redis.delete(f"inflight_stop:{dep_id}")  # noqa: SLF001

    # Codex iter 18 P2: a flat stop_report alone is INSUFFICIENT — the
    # child could write the report and then hang in dispose() before
    # the terminal LiveDeployment.status / live_node_processes terminal
    # update lands. ``/stop`` already mirrors this guard (live.py:1582).
    # For each successfully-stopped deployment, poll the row to a
    # terminal status BEFORE counting it toward drain success. If the
    # poll times out, the deployment is tracked as ``any_non_flat`` so
    # the operator sees the 207 even on a clean flatness report.
    terminal_results: dict[str, bool] = {}
    if flatness_nonces:

        async def _poll_terminal_one(dep_uuid: UUID) -> tuple[str, bool]:
            row = await _poll_for_terminal(
                db,
                dep_uuid,
                ready_statuses=frozenset(),
                terminal_statuses=frozenset({"stopped", "failed"}),
                timeout_s=STOP_POLL_TIMEOUT_S,
                interval_s=START_POLL_INTERVAL_S,
            )
            return str(dep_uuid), row is not None

        # Per-deployment poll. Sequential (not parallel) because the
        # helper rolls back the shared session between iterations —
        # gathering it would race on the AsyncSession.
        for dep_uuid_str in flatness_nonces:
            dep_uuid_str, terminal_seen = await _poll_terminal_one(UUID(dep_uuid_str))
            terminal_results[dep_uuid_str] = terminal_seen

        # Codex iter 20 P2: ``_poll_for_terminal`` only confirms the
        # ``live_node_processes`` row reached a terminal status. ``/stop``
        # also updates the parent ``LiveDeployment.status='stopped'`` so
        # ``/status`` reflects the drained state. Mirror that here for
        # every confirmed-terminal deployment so ``/api/v1/live/status``
        # doesn't keep showing drained accounts as ``running`` even
        # though the subprocess has exited.
        terminal_now = datetime.now(UTC)
        synced_any = False
        # INVARIANT 2 (council 2026-06-01 follow-up — DETERMINISTIC TOTAL ORDER):
        # iterate the to-sync deployments in ascending-id order so the per-row
        # dirty-flush UPDATE (which acquires a deployment row write lock held
        # until commit) acquires its locks in the SAME global id order the
        # supervisor sweeps use. A drain on account A and a stale sweep can have
        # OVERLAPPING deployment sets (a drained account's deployment is also
        # stale); locking in id order means they serialise rather than AB-BA
        # deadlock. (Two drains target different account_ids → disjoint sets, but
        # the cross-locker overlap with the sweeps makes the ordering load-bearing.)
        terminal_dep_uuids = sorted(
            (UUID(s) for s, seen in terminal_results.items() if seen),
        )
        for dep_uuid in terminal_dep_uuids:
            dep_row = await db.get(LiveDeployment, dep_uuid)
            if dep_row is not None and dep_row.status not in ("stopped", "failed"):
                dep_row.status = "stopped"
                dep_row.last_stopped_at = terminal_now
                synced_any = True
        if synced_any:
            await db.commit()

    def _drain_summary(dep_id: str) -> dict[str, Any]:
        report = flatness_results.get(dep_id)
        return {
            "deployment_id": dep_id,
            "stop_nonce": flatness_nonces[dep_id],
            "broker_flat": report["broker_flat"] if report else None,
            "remaining_positions": report["remaining_positions"] if report else [],
            "terminal_confirmed": terminal_results.get(dep_id, False),
        }

    flatness_summary = [_drain_summary(dep_id) for dep_id in flatness_nonces]
    any_non_flat = any(
        f["broker_flat"] is False or f["broker_flat"] is None or not f["terminal_confirmed"]
        for f in flatness_summary
    )

    log.info(
        "live_drain_account",
        extra={
            "account_id": account_id,
            "stopped_count": len(stopped),
            "failed_count": len(failed),
            "any_non_flat": any_non_flat,
            "user_id": str(user_id) if user_id else None,
        },
    )

    body: dict[str, Any] = {
        "account_id": account_id,
        "stopped": stopped,
        "failed": failed,
        "flatness_reports": flatness_summary,
        "any_non_flat": any_non_flat,
        "halt_cause": HaltCause.OPERATOR_DRAIN.value,
    }

    if failed and not stopped:
        # Total publish failure — surface as 503 with a structured error
        # envelope. The halt latch IS set (the early Redis SET is the
        # authoritative drain marker), so /start for this account_id will
        # still be blocked at the API layer; the 503 tells the operator
        # the running deployments need manual attention.
        return JSONResponse(
            status_code=503,
            content={
                **body,
                "error": {
                    "code": "DRAIN_PARTIAL_FAILURE",
                    "message": (
                        f"Failed to publish STOP to {len(failed)} deployment(s) "
                        f"for account_id={account_id}. None were stopped. Halt "
                        "latch IS set; running deployments need manual attention."
                    ),
                },
            },
        )
    if failed:
        # Partial failure — 207 Multi-Status. Both arrays populated.
        return JSONResponse(
            status_code=207,
            content={
                **body,
                "error": {
                    "code": "DRAIN_PARTIAL_FAILURE",
                    "message": (
                        f"Partial drain for account_id={account_id}: "
                        f"{len(stopped)} stopped, {len(failed)} failed. "
                        "Halt latch IS set."
                    ),
                },
            },
        )
    if any_non_flat:
        # Codex iter 14 P2: STOP commands published successfully BUT the
        # supervisor either didn't report flatness within 35s or reported
        # remaining positions. The drain is NOT complete — surface 207
        # so operators see the unknown-state truth instead of a green
        # 200 that masks unflattened positions.
        return JSONResponse(
            status_code=207,
            content={
                **body,
                "error": {
                    "code": "DRAIN_INCOMPLETE_FLATNESS",
                    "message": (
                        f"STOP commands published for {len(stopped)} deployment(s) "
                        f"on account_id={account_id}, but flatness reports show "
                        "remaining positions or did not arrive within the deadline. "
                        "Verify in the IB portal; halt latch IS set."
                    ),
                },
            },
        )
    return body


def _resume_blocked_data_stale(message: str) -> NoReturn:
    """Raise a 409 ``RESUME_BLOCKED_DATA_STALE`` (PR 1b T6 fail-closed gate).

    Uses the project error envelope (``{error: {code, message}}``) and
    ``HTTPException`` so FastAPI renders the 409 — the fleet halt latch stays
    set and NOTHING was cleared."""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": {"code": "RESUME_BLOCKED_DATA_STALE", "message": message}},
    )


def _resume_blocked_reconciliation(deployment_id: str) -> NoReturn:
    """Raise a 409 ``RESUME_BLOCKED_RECONCILIATION`` (PR 1b T6 fail-closed gate).

    An active deployment is missing its reconciliation marker — the node is not
    proven to be genuinely running, so resume refuses and clears nothing."""
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": {
                "code": "RESUME_BLOCKED_RECONCILIATION",
                "message": (
                    f"deployment {deployment_id} has no reconciliation marker — "
                    "node not proven reconciled; refusing to resume"
                ),
            }
        },
    )


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

    PR 1b T6 — FAIL-CLOSED preconditions. Before clearing ANYTHING the
    route verifies, for every ACTIVE deployment, that (a) the data-stale
    monitor's required-feed manifest is present (an ABSENT manifest means
    the monitor never started / its node is gone → refuse), (b) every
    manifest feed has a present, TTL-alive freshness row whose verdict is
    ``warm`` (a ``pending`` feed — no data observed yet within startup grace —
    blocks with a distinct "no data observed yet" message; ``stale``/absent
    blocks too), and (c) the node still carries a reconciliation marker. A
    failure returns 409 (``RESUME_BLOCKED_DATA_STALE`` /
    ``RESUME_BLOCKED_RECONCILIATION``) and clears NOTHING. The success
    path then re-verifies the SAME preconditions atomically inside
    ``RESUME_CLEAR_LUA`` (closing the monitor-death race between this
    probe and the clear) before deleting the halt keyset. An EMPTY
    manifest (legacy / non-Databento node) is vacuously warm for
    freshness but still requires the reconciliation marker; zero active
    deployments is a vacuous pass.
    """
    user_id = await _resolve_user_id(db, claims)

    # (1) Load ACTIVE deployments using the canonical 5-tuple (incl.
    # 'stopping') — a node mid-teardown still holds IB positions.
    active_deployments = (
        (
            await db.execute(
                select(LiveDeployment).where(LiveDeployment.status.in_(ACTIVE_DEPLOYMENT_STATUSES))
            )
        )
        .scalars()
        .all()
    )

    # Probe each active deployment's freshness manifest + per-feed verdicts +
    # reconciliation marker. Build the Lua key lists as we go so the atomic
    # clear re-verifies EXACTLY what passed here.
    manifest_keys: list[str] = []
    expected_manifests: list[str] = []
    verdict_keys: list[str] = []
    reconciled_keys: list[str] = []
    feeds_verified: list[str] = []
    reconciled_verified: list[str] = []

    for dep in active_deployments:
        dep_id = str(dep.id)
        manifest_key = data_freshness_manifest_key(dep_id)
        raw_manifest = await bus._redis.get(manifest_key)  # noqa: SLF001
        if raw_manifest is None:
            # ABSENT manifest → monitor never started (or node gone + TTL
            # lapsed). Fail-closed: we cannot prove the feeds are warm.
            return _resume_blocked_data_stale(
                f"deployment {dep_id} has no data-freshness manifest "
                "(monitor not running) — cannot prove feeds are warm"
            )
        try:
            manifest = json.loads(raw_manifest)
        except (ValueError, TypeError):
            return _resume_blocked_data_stale(
                f"deployment {dep_id} freshness manifest is malformed"
            )
        # Fail-closed shape validation: a non-list manifest, or any entry that
        # is not a dict carrying both 'dataset' and 'feed', is treated as a
        # malformed manifest and BLOCKS the resume (409) — never a 500. We
        # cannot prove the listed feeds are warm if we can't read their shape.
        if not isinstance(manifest, list) or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("dataset"), str)
            and entry.get("dataset")
            and isinstance(entry.get("feed"), str)
            and entry.get("feed")
            for entry in manifest
        ):
            return _resume_blocked_data_stale(
                f"deployment {dep_id} freshness manifest is malformed"
            )

        manifest_keys.append(manifest_key)
        # Pin the EXACT raw value Python validated (content-pin closing the
        # changed-universe TOCTOU). ``bus._redis`` decodes responses, so
        # ``raw_manifest`` is the str the monitor wrote; coerce bytes defensively
        # in case a future bus uses a byte client. The Lua compares this to the
        # CURRENT manifest value before clearing on the verdict-key list we
        # derived FROM it.
        expected_manifests.append(
            raw_manifest.decode() if isinstance(raw_manifest, bytes) else raw_manifest
        )
        reconciled_marker = reconciled_key(dep_id)
        reconciled_keys.append(reconciled_marker)

        # (3) reconciliation marker MUST be present for every active node.
        if not await bus._redis.exists(reconciled_marker):  # noqa: SLF001
            return _resume_blocked_reconciliation(dep_id)
        reconciled_verified.append(dep_id)

        # (2) Non-empty manifest → every listed feed must have a present
        # (TTL-alive) verdict row equal to 'warm'. EMPTY manifest → vacuous.
        # PR 1b T6 / Codex iter-2 P1: 'pending' (no data observed yet, within
        # startup grace) is BLOCKING — distinct from stale/absent so the
        # operator sees that the feed simply hasn't delivered any data yet
        # (e.g. a node restart DURING an ongoing outage). 'warm' is the ONLY
        # resumable verdict; the same 409 RESUME_BLOCKED_DATA_STALE code covers
        # both, but the message names which.
        stale_feeds: list[str] = []
        pending_feeds: list[str] = []
        for entry in manifest:
            dataset = entry["dataset"]
            native_bar_type = entry["feed"]
            feed_label = f"{dep_id}:{dataset}:{native_bar_type}"
            verdict_key = data_freshness_key(dep_id, dataset, native_bar_type) + VERDICT_KEY_SUFFIX
            verdict = await bus._redis.get(verdict_key)  # noqa: SLF001
            if verdict == "pending":
                pending_feeds.append(feed_label)
                continue
            if verdict is None or verdict != "warm":
                stale_feeds.append(feed_label)
                continue
            verdict_keys.append(verdict_key)
            feeds_verified.append(feed_label)
        if pending_feeds:
            return _resume_blocked_data_stale(
                "refusing to resume — no data observed yet (within startup "
                "grace) for feed(s): " + ", ".join(pending_feeds)
            )
        if stale_feeds:
            return _resume_blocked_data_stale(
                "refusing to resume — stale/absent feed(s): " + ", ".join(stale_feeds)
            )

    # Success-path CLEAR: re-verify the preconditions atomically and delete the
    # halt keyset only if every check still holds. ``delete_keys`` are the halt
    # latch + companions + cause + history + legacy transition-compat keys.
    delete_keys = [
        _HALT_KEY,
        f"{_HALT_KEY}:set_by",
        f"{_HALT_KEY}:set_at",
        halt_cause_key("fleet"),
        f"{halt_cause_key('fleet')}:history",
        # Legacy transition-compat keys a pre-T5 node left on the latch key
        # itself (``disconnect_handler._HALT_REASON_KEY`` /
        # ``_HALT_SOURCE_KEY`` = ``msai:risk:halt:reason`` / ``:source``).
        # Derive from the fleet latch key — NOT ``halt_cause_key`` (which is
        # the ``:cause`` namespace) — so the clear actually targets the keys
        # those nodes wrote.
        f"{_HALT_KEY}:reason",
        f"{_HALT_KEY}:source",
    ]
    lua_keys = [*manifest_keys, *verdict_keys, *reconciled_keys, *delete_keys]
    # ARGV: 3 counts, then the expected raw manifest VALUE per manifest key (in
    # KEYS order) — the content-pin closing the changed-universe TOCTOU.
    lua_argv = [
        str(len(manifest_keys)),
        str(len(verdict_keys)),
        str(len(reconciled_keys)),
        *expected_manifests,
    ]
    result = await bus._redis.eval(  # type: ignore[misc]  # noqa: SLF001
        RESUME_CLEAR_LUA, len(lua_keys), *lua_keys, *lua_argv
    )
    result_str = result.decode() if isinstance(result, bytes) else str(result)
    if result_str != "OK":
        # A precondition re-staled between the Python probe and the clear
        # (monitor-death race / feed flipped to stale / manifest feed universe
        # changed). Nothing was deleted.
        reason, _, offending = result_str.partition(":")
        if reason == "RECONCILED_MISSING":
            return _resume_blocked_reconciliation(offending or "(unknown)")
        if reason == "MANIFEST_CHANGED":
            return _resume_blocked_data_stale(
                "refusing to resume — manifest changed during resume "
                f"(feed universe differs from the probed value) on key {offending}; "
                "the monitor re-published a different required-feed set — retry resume"
            )
        return _resume_blocked_data_stale(
            f"refusing to resume — precondition re-check failed ({reason}) on key {offending}"
        )

    # PR #65 Codex P2 round-4: clear all `stop_unknown:*` markers on
    # /resume. After the operator has verified IB-portal positions
    # and is ready to re-deploy, the flatness-unknown state should
    # be cleared so subsequent /stop calls don't keep returning 504.
    # SCAN avoids blocking Redis on a KEYS call (production-safe).
    unknown_cleared = 0
    try:
        async for key in bus._redis.scan_iter(match="stop_unknown:*"):  # noqa: SLF001
            await bus._redis.delete(key)  # noqa: SLF001
            unknown_cleared += 1
    except Exception:  # noqa: BLE001
        log.exception("stop_unknown_clear_failed")

    await log_audit(
        db,
        user_id=user_id,
        action="live_resume",
        resource_type="live_deployment",
        details={
            "active_deployments_checked": len(active_deployments),
            "feeds_verified": feeds_verified,
            "reconciled_verified": reconciled_verified,
            "stop_unknown_cleared": unknown_cleared,
        },
    )

    log.warning(
        "kill_switch_resumed",
        resumed_by=str(user_id),
        active_deployments_checked=len(active_deployments),
        feeds_verified=len(feeds_verified),
    )

    return LiveResumeResponse(
        resumed=True,
        resumed_by=str(user_id) if user_id else None,
        verified=ResumeVerifiedPreconditions(
            active_deployments_checked=len(active_deployments),
            feeds_verified=feeds_verified,
            reconciled_verified=reconciled_verified,
        ),
    )


@router.post("/resume/{account_id}", response_model=None)
async def live_resume_account(
    account_id: str,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
) -> dict[str, Any]:
    """Clear the account-scoped halt latch (Codex iter 6 P1-2).

    Mirror of ``/resume`` (which clears the fleet-wide halt) but scoped
    to a single account. Without this endpoint, ``/drain/{account_id}``
    sets a 24h halt latch and the operator has no API to undo it short
    of waiting for TTL or editing Redis by hand.

    Idempotent: returns ``resumed=True`` whether or not the latch was
    actually set. Audit log captures whether the latch existed.
    """
    account_id = _normalize_account_id_path_param(account_id)

    user_id = await _resolve_user_id(db, claims)

    # Batch the latch + cause-companion deletes through a transactional
    # pipeline so a mid-sequence Redis error can't leave one without the
    # other (mirrors the /kill-all + /drain pipeline pattern).
    async with bus._redis.pipeline(transaction=True) as _resume_pipe:  # noqa: SLF001
        _resume_pipe.delete(account_halt_key(account_id))
        _resume_pipe.delete(halt_cause_key("account", account_id=account_id))
        results = await _resume_pipe.execute()
    deleted_count = sum(int(r) for r in results if isinstance(r, int))

    await log_audit(
        db,
        user_id=user_id,
        action="live_resume_account",
        resource_type="live_deployment",
        details={"account_id": account_id, "keys_deleted": deleted_count},
    )

    log.warning(
        "account_halt_resumed",
        extra={
            "account_id": account_id,
            "resumed_by": str(user_id) if user_id else "operator",
            "keys_deleted": deleted_count,
        },
    )

    return {"resumed": True, "account_id": account_id, "keys_deleted": deleted_count}


@router.get("/data-health", response_model=DataHealthResponse)
async def live_data_health(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
) -> DataHealthResponse:
    """Operator read-only window onto the in-node data-stale monitor (PR 1b T7).

    Built MANIFEST-FIRST per active deployment (same ``ACTIVE_DEPLOYMENT_STATUSES``
    + manifest reader as ``/resume``): returns every required Databento feed with
    its freshness row (account/node/deployment/dataset/feed/symbol + last_event_ts,
    phase, grace_s, verdict, published_at). A manifest feed with NO live freshness
    row is reported with a derived ``missing`` verdict — never silently absent. An
    empty fleet yields ``feeds: []`` with a 200. The fleet halt latch + parsed
    halt-cause JSON are included for operator context.

    This shares ``hydrate_data_health_metrics`` with the ``/metrics`` scrape path,
    so a call here also refreshes the labeled feed-health gauges. A Redis/DB blip
    degrades to whatever the reader produced — it never 500s the operator."""
    from msai.services.observability.data_health import hydrate_data_health_metrics

    snapshot = await hydrate_data_health_metrics(db, bus._redis)  # noqa: SLF001
    return DataHealthResponse(**snapshot.as_dict())


@router.get("/status")
async def live_status(
    active_only: bool = False,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
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

    # PR 2 T8: fetch the latest live_node_processes row per deployment in a
    # single query (no N+1), so each row can surface its restart-authority
    # health (auto_restart_paused, consecutive_respawn_failures, …).
    latest_process = await _latest_process_by_deployment(db, [d.id for d in deployments])

    # Read the *persistent* halt flag from Redis (the key written by
    # ``/kill-all`` and cleared by ``/resume``) rather than the
    # process-local ``_risk_engine.is_halted`` attribute. The
    # in-memory flag does not survive backend restarts and does not
    # propagate across replicas, so the UI's Resume button vanishes
    # on reload after a kill-all. Reading Redis here ensures the
    # status reflects platform truth — see T0a in
    # docs/plans/2026-05-15-live-deployment-workflow-ui-cli.md.
    #
    # Codex iter-9 P2: handle direct-call paths (integration tests that
    # invoke ``live_status(claims=..., db=session)`` without going
    # through FastAPI's DI). When ``bus`` was not resolved, it's still
    # the ``Depends(...)`` sentinel object and ``_halt_is_active`` would
    # crash on ``bus._redis``. Type-check before reading. False is the
    # documented test fallback (the direct-call test doesn't assert
    # ``risk_halted``; over-the-wire calls always pass a real bus).
    bus_resolved = isinstance(bus, LiveCommandBus)
    risk_halted = await _halt_is_active(bus) if bus_resolved else False
    # PR 2 T8: fleet-halt latch + router heartbeat age are fleet-wide reads
    # done ONCE. ``risk_halted`` already reflects the fleet halt key, so
    # reuse it for ``fleet_halted`` (same latch) without a second round-trip.
    fleet_halted = risk_halted
    router_heartbeat_age_s = await bus.read_router_heartbeat_age_s() if bus_resolved else None
    # Per-account halt latch: read once per distinct account_id (a fleet may
    # have many deployments under the same account) and reuse across rows.
    account_halt_cache: dict[str, bool] = {}

    items: list[LiveDeploymentInfo] = []
    for d in deployments:
        account_halted = False
        if bus_resolved and d.account_id:
            if d.account_id not in account_halt_cache:
                account_halt_cache[d.account_id] = await _account_halt_is_active(bus, d.account_id)
            account_halted = account_halt_cache[d.account_id]
        restart_authority = _restart_authority_fields(
            latest_process.get(d.id),
            fleet_halted=fleet_halted,
            account_halted=account_halted,
        )
        items.append(
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
                # PR 1 T14 — account context for the fleet topology.
                # ``ibg_client_id`` is deterministically re-derived from the
                # deployment_slug via the shared helper (no DB column needed).
                account_id=d.account_id,
                ib_login_key=d.ib_login_key,
                # Task 5 — control-plane broker-account linkage (NULL for
                # pre-registry / legacy deployments).
                broker_account_id=d.broker_account_id,
                ibg_client_id=(
                    derive_ibg_client_id(d.deployment_slug) if d.deployment_slug else None
                ),
                # PR 2 T8 — per-account restart-authority health.
                last_heartbeat_at=(
                    p.last_heartbeat_at if (p := latest_process.get(d.id)) else None
                ),
                **restart_authority,
            )
        )

    return LiveStatusResponse(
        deployments=items,
        risk_halted=risk_halted,
        active_count=_node_manager.active_count,
        router_heartbeat_age_s=router_heartbeat_age_s,
    )


@router.get("/status/{deployment_id}", response_model=LiveDeploymentStatusResponse)
async def get_live_deployment_status(
    deployment_id: UUID,
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008, ARG001
    db: AsyncSession = Depends(get_db),  # noqa: B008
    bus: LiveCommandBus = Depends(get_command_bus),  # noqa: B008
) -> LiveDeploymentStatusResponse:
    """Return the current status of a single live deployment (Task 1.13).

    Reads from the database — does NOT maintain or consult any
    in-memory state. The logical ``LiveDeployment`` row is joined with
    the most recent ``LiveNodeProcess`` row so the caller sees both
    the stable identity (slug, trader_id, config hash) AND the live
    per-run state (pid, host, heartbeat, terminal outcome).

    PR 2 T8: also surfaces the latest process row's restart-authority
    health + the fleet/account halt-latch state (read from Redis via the
    command bus) so the per-deployment drill-in carries the SAME
    restart-authority view as the ``/live/status`` list (UC-API-1).

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

    # PR 2 T8 — halt-latch state, read live from Redis. Same DI-sentinel
    # guard as the list endpoint: a direct-call path (no FastAPI DI) leaves
    # ``bus`` as the ``Depends`` sentinel, in which case halts default False.
    bus_resolved = isinstance(bus, LiveCommandBus)
    fleet_halted = await _halt_is_active(bus) if bus_resolved else False
    account_halted = (
        await _account_halt_is_active(bus, deployment.account_id)
        if (bus_resolved and deployment.account_id)
        else False
    )
    restart_authority = _restart_authority_fields(
        process,
        fleet_halted=fleet_halted,
        account_halted=account_halted,
    )

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
        broker_account_id=deployment.broker_account_id,
        process_id=process.id if process else None,
        pid=process.pid if process else None,
        host=process.host if process else None,
        process_status=process.status if process else None,
        last_heartbeat_at=process.last_heartbeat_at if process else None,
        exit_code=process.exit_code if process else None,
        error_message=process.error_message if process else None,
        failure_kind=process.failure_kind if process else None,
        **restart_authority,
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


# iter-5 verify-e2e P2-2: Nautilus stores side as an enum int (1=BUY,
# 2=SELL) and audit/trade rows propagate it. Surfacing the raw integer
# to the UI rendered an unintelligible "1"/"2" badge — translate at the
# API boundary so /trades and /audits both return human-readable strings.
_ORDER_SIDE_LABELS: dict[str, str] = {"1": "BUY", "2": "SELL"}


def _audit_side_label(raw: str | int) -> str:
    """Coerce a Nautilus OrderSide enum int (stored as str in audit rows)
    to the human-readable BUY / SELL label. Unknown values pass through
    unchanged so the UI can flag any new enum values cleanly."""
    return _ORDER_SIDE_LABELS.get(str(raw), str(raw))


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
            "side": _audit_side_label(r.side),
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
                "side": _audit_side_label(r.side),
                "quantity": str(r.quantity),
                "status": r.status,
                "strategy_code_hash": r.strategy_code_hash,
                "timestamp": r.ts_attempted.isoformat(),
            }
            for r in rows
        ]
    }
