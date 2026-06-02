"""Pydantic schemas for live trading API endpoints."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime
from typing import Any
from uuid import UUID  # noqa: TC003 — Pydantic resolves annotations at runtime

from pydantic import BaseModel, Field, field_validator


class LiveStartRequest(BaseModel):
    """Request schema for starting a live or paper trading deployment."""

    strategy_id: UUID
    config: dict[str, Any]
    instruments: list[str]
    paper_trading: bool = True


class PortfolioStartRequest(BaseModel):
    """Request schema for starting a portfolio-based live deployment.

    Instead of deploying a single strategy (like :class:`LiveStartRequest`),
    this deploys an entire frozen portfolio revision — a set of strategies
    with weights, configs, and instruments — to a specific IB account.
    """

    portfolio_revision_id: UUID
    account_id: str
    paper_trading: bool = True
    ib_login_key: str = Field(min_length=1, max_length=64)

    @field_validator("account_id")
    @classmethod
    def _normalize_account_id(cls, v: str) -> str:
        """Codex iter 16 P2: strip whitespace and reject internal
        whitespace so the halt-latch key written by ``/drain/{account_id}``
        (URL-stripped) always matches the key read by the supervisor's
        per-account halt check. A leading-space ``" DUP733214"`` slipping
        past would let the supervisor read a different Redis key than
        the drain endpoint wrote, bypassing the drain latch.
        """
        normalized = v.strip()
        if not normalized:
            raise ValueError("account_id cannot be empty / whitespace-only")
        if any(ch.isspace() for ch in normalized):
            raise ValueError(
                f"account_id={v!r} contains whitespace — IB account ids "
                "are alphanumeric (e.g. 'DUP733214' or 'U1234567')"
            )
        return normalized

    # Codex iter 15 P2 REVERTED in iter 20 P2: the original validator
    # rejected ``ib_login_key='default'`` to prevent operator typos from
    # silently bypassing ``GatewayRouter.resolve``. But warm-restarting
    # a deployment whose row was backfilled by migration ``t8o9p0q1r2s3``
    # REQUIRES sending ``default`` to match the existing
    # ``identity_signature`` — sending a real key creates a different
    # identity and hits the ``(portfolio_revision_id, account_id)``
    # conflict path. So the schema validator made legacy rows
    # unrestartable through the public API. The supervisor's
    # ``is_routed`` predicate (live_supervisor/__main__.py) and startup
    # warning already handle the typo concern: ``default`` falls back
    # to ``settings.ib_host/port`` and the supervisor emits a clear
    # warning at boot when GATEWAY_CONFIG is empty. Operator-typo
    # protection ultimately belongs at the operator's CONFIG layer
    # (GATEWAY_CONFIG present in prod → no fall-through unless
    # explicitly intended).


class LiveStopRequest(BaseModel):
    """Request schema for stopping a running deployment."""

    deployment_id: UUID


class LiveDeploymentInfo(BaseModel):
    """Summary of a single live deployment."""

    id: UUID
    strategy_id: UUID | None = None
    status: str
    paper_trading: bool
    instruments: list[str] = []
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    # PR 1 T14 — account context for the fleet topology. ``ib_login_key``
    # + ``account_id`` are sourced from the LiveDeployment row; the
    # ``ibg_client_id`` is the deterministic derivation from
    # ``deployment_slug`` via ``msai.services.nautilus.ibg_client_id``
    # (the SoT for client-id derivation). Tests / CLI / UI consume these
    # to correlate logs across the fleet.
    account_id: str | None = None
    ib_login_key: str | None = None
    ibg_client_id: int | None = None

    # PR 2 T8 — per-account restart-authority health (additive, read-only).
    # Sourced from the latest ``live_node_processes`` row for this deployment
    # (the four restart-authority columns added in T1) plus the live Redis
    # halt latches. All nullable so a deployment that has never spawned a
    # node process (no live_node_processes row) still serializes cleanly.
    #
    # ``auto_restart_paused`` True => the bounded restart policy tripped and
    # the reaper will NOT auto-respawn this account; an operator must
    # intervene. ``consecutive_respawn_failures`` + ``last_restart_at`` let
    # the operator see how close an account is to (or how it crossed) the
    # ceiling. ``last_heartbeat_age_s`` is derived server-side from
    # ``last_heartbeat_at`` so the UI/CLI don't have to clock-compare.
    # ``fleet_halted`` / ``account_halted`` reflect the fleet + account halt
    # latches (read from Redis, account-scoped per ``account_halt_key``).
    auto_restart_paused: bool | None = None
    auto_restart_pause_reason: str | None = None
    consecutive_respawn_failures: int | None = None
    last_restart_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_heartbeat_age_s: float | None = None
    fleet_halted: bool = False
    account_halted: bool = False

    model_config = {"from_attributes": True}


class LiveStatusResponse(BaseModel):
    """Response schema for the live status endpoint."""

    deployments: list[LiveDeploymentInfo]
    risk_halted: bool
    active_count: int
    # PR 2 T8 — supervisor-liveness signal. Age in seconds of the
    # ``router_heartbeat`` Redis key the supervisor stamps every loop pass
    # (the SAME key the /start-portfolio 503 gate and the SPOF alert read —
    # NOT a second heartbeat source). ``None`` means the key is absent /
    # expired (supervisor down or never started — fail-closed). A small age
    # confirms the single-supervisor SPOF is alive.
    router_heartbeat_age_s: float | None = None


class LiveDeploymentStatusResponse(BaseModel):
    """Response schema for ``GET /api/v1/live/status/{deployment_id}``.

    Combines the stable ``LiveDeployment`` row (logical record — survives
    restarts, keyed by ``identity_signature``) with the most recent
    ``LiveNodeProcess`` row (per-restart run record — pid, heartbeat,
    terminal outcome). Returning both lets the UI show "this deployment
    is running as pid 12345 on host box-3 with last heartbeat 1.2 s ago"
    without hitting the supervisor directly.

    Process fields (``pid``, ``host``, ``process_status``, etc.) are
    nullable because a deployment that has never run (or whose newest
    process row has been garbage-collected) has no live row.
    """

    # Logical deployment fields
    id: UUID
    strategy_id: UUID | None = None
    deployment_slug: str
    status: str
    paper_trading: bool
    instruments: list[str] = []
    last_started_at: datetime | None = None
    last_stopped_at: datetime | None = None

    # Latest per-run process fields — nullable when no live_node_processes
    # row exists for this deployment.
    process_id: UUID | None = None
    pid: int | None = None
    host: str | None = None
    process_status: str | None = None
    last_heartbeat_at: datetime | None = None
    exit_code: int | None = None
    error_message: str | None = None
    failure_kind: str | None = None

    # PR 2 T8 — per-account restart-authority health (additive, read-only).
    # Mirrors the fields on ``LiveDeploymentInfo`` so the per-deployment
    # drill-in GET surfaces the same restart-authority view as the list
    # endpoint (UC-API-1: "the per-deployment detail GET returns the same
    # account's restart-authority fields"). Sourced from the latest
    # ``live_node_processes`` row + the fleet/account Redis halt latches.
    auto_restart_paused: bool | None = None
    auto_restart_pause_reason: str | None = None
    consecutive_respawn_failures: int | None = None
    last_restart_at: datetime | None = None
    last_heartbeat_age_s: float | None = None
    fleet_halted: bool = False
    account_halted: bool = False

    model_config = {"from_attributes": True}


class LiveKillAllResponse(BaseModel):
    """Response schema for the kill-all emergency endpoint.

    ``stopped`` is the count of stop commands SUCCESSFULLY
    published to the supervisor command bus. ``failed_publish``
    is the count of active deployments where the publish
    raised — these are NOT acknowledged by the supervisor and
    require manual intervention. ``risk_halted`` is always True
    after a kill-all because the persistent halt flag is set
    unconditionally as Layer 1, BEFORE any publishes. Codex
    batch 9 P1: an emergency-stop endpoint must NOT silently
    swallow failures — operators need to see them.
    """

    stopped: int
    failed_publish: int = 0
    risk_halted: bool
    # PR #65 Codex P2: surface broker flatness directly in the response.
    # `any_non_flat` is True if ANY deployment's stop_report came back
    # `broker_flat=False`, OR if any flatness poll timed out / never
    # arrived (operator can't distinguish "still has positions" from
    # "we don't know" — both demand IB-portal verification). When True
    # the operator MUST verify residual positions via IB portal before
    # `/resume`. The audit log carries the per-deployment detail; the
    # response body keeps just the boolean so a panic-button caller
    # doesn't have to inspect audit history to know.
    any_non_flat: bool = False
    flatness_reports: list[dict[str, Any]] = []


class LiveResumeResponse(BaseModel):
    """Response schema for the resume endpoint that clears the
    persistent halt flag set by ``/kill-all``."""

    resumed: bool


class StrategyMemberInfo(BaseModel):
    """Per-strategy member detail within a portfolio deployment."""

    strategy_id: UUID
    strategy_id_full: str
    instruments: list[str]
    weight: str

    model_config = {"from_attributes": True}


class PortfolioDeploymentInfo(BaseModel):
    """Summary of a portfolio-based deployment with per-member detail."""

    id: UUID
    portfolio_revision_id: UUID | None = None
    account_id: str
    status: str
    paper_trading: bool
    deployment_slug: str
    members: list[StrategyMemberInfo] = []

    model_config = {"from_attributes": True}


class LivePositionsResponse(BaseModel):
    """Response schema for current open positions."""

    positions: list[dict[str, Any]]


class LiveTradesResponse(BaseModel):
    """Response schema for recent live trade executions."""

    trades: list[dict[str, Any]]
    total: int
