"""Account API router -- IB account summary, portfolio, and health.

Provides endpoints to query Interactive Brokers account data and
gateway connectivity status. The summary + portfolio endpoints are
served from the long-lived :class:`IBAccountSnapshot` cache (one IB
connection, refreshed every 30 s in the background); the health
endpoint is served from the existing :class:`IBProbe`.

Two background tasks must be running for these endpoints to return
meaningful state:

* ``ib_probe`` — TCP-level health check (used by ``/health``).
* ``ib_account_snapshot`` — IB-API connection + account/portfolio
  refresh (used by ``/summary`` and ``/portfolio``).

Both are started by the FastAPI lifespan in
:mod:`msai.main` via :func:`start_ib_probe_task` and
:func:`start_ib_account_snapshot`. Without those calls the endpoints
keep returning the zero-state shape — same fail-soft behaviour the
per-request implementation had.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.logging import get_logger
from msai.services.ib_account_snapshot import (
    IBAccountSnapshot,
    get_snapshot,
)
from msai.services.ib_probe import IBProbe

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/account", tags=["account"])

# Module-level singletons (per process). The probe is created here
# but its periodic loop is started by ``start_ib_probe_task`` from
# the FastAPI lifespan in ``main.py`` — without that the probe
# never runs ``check_health`` and the endpoint always reports
# ``gateway_connected=false`` regardless of actual IB state. Drill
# 2026-04-15 misled me three times because of this.
_ib_probe = IBProbe(host=settings.ib_host, port=settings.ib_port)

_probe_task: asyncio.Task[None] | None = None
"""The background task running ``IBProbe.run_periodic``. Held at
module scope so ``stop_ib_probe_task`` can cancel it cleanly on
shutdown. ``None`` whenever the task is not running."""

# Probe interval: 30 s matches the heartbeat cadence used elsewhere
# in the live-trading stack and keeps the IB Gateway connection
# pool churn low. Override via ``IB_PROBE_INTERVAL_S`` env var if a
# specific deployment needs faster detection of disconnects.
_PROBE_INTERVAL_S: int = 30


async def start_ib_probe_task() -> None:
    """Spawn the background probe task on FastAPI startup.

    Idempotent — calling twice in a row leaves the original task
    running and logs a warning. The task is required for the
    ``/api/v1/account/health`` endpoint to return meaningful state.
    """
    global _probe_task  # noqa: PLW0603
    if _probe_task is not None and not _probe_task.done():
        log.warning("ib_probe_task_already_running")
        return
    _probe_task = asyncio.create_task(
        _ib_probe.run_periodic(interval=_PROBE_INTERVAL_S),
        name="ib_probe_periodic",
    )
    log.info("ib_probe_task_started", interval_s=_PROBE_INTERVAL_S)


async def stop_ib_probe_task() -> None:
    """Cancel and await the probe task on FastAPI shutdown.

    Always resets the probe's cached state (``is_healthy``,
    ``consecutive_failures``) even when no task is running. That
    way a subsequent ``start_ib_probe_task`` begins from a clean
    slate — Codex review P3: without the reset a stop/start cycle
    in the same process would leak the previous cycle's status
    for up to one probe interval.
    """
    global _probe_task  # noqa: PLW0603
    task_was_running = _probe_task is not None and not _probe_task.done()
    if task_was_running:
        assert _probe_task is not None  # for mypy
        _probe_task.cancel()
        # iter-3 SF P3: narrow the swallow to match
        # ``IBAccountSnapshot.stop()`` — suppress CancelledError (expected
        # on cancel + drain) but log any other exception with type so a
        # real probe-task crash leaves a forensic trail.
        try:
            await _probe_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ib_probe_task_stop_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
    _probe_task = None

    _ib_probe._is_healthy = False  # noqa: SLF001
    _ib_probe._consecutive_failures = 0  # noqa: SLF001

    if task_was_running:
        log.info("ib_probe_task_stopped")


async def start_ib_account_snapshot() -> None:
    """Start the singleton account snapshot's refresh loop.

    Lazy-creates the snapshot on first call (via
    :func:`msai.services.ib_account_snapshot.get_snapshot`) and spawns
    its background refresh task. The call is non-blocking — IB
    Gateway connection happens **inside** the loop body so FastAPI
    boots cleanly even when IB Gateway is down.
    """
    snapshot = get_snapshot()
    snapshot.start()


async def stop_ib_account_snapshot() -> None:
    """Stop the singleton snapshot's refresh loop on shutdown.

    Cancels the background task and disconnects the underlying IB
    client. Always safe to call: a missing task or already-closed
    socket is suppressed.
    """
    snapshot = get_snapshot()
    await snapshot.stop()


def _get_snapshot_dep() -> IBAccountSnapshot:
    """FastAPI dependency wrapper around :func:`get_snapshot`.

    Pulled into its own function so individual tests can override
    ``app.dependency_overrides[_get_snapshot_dep]`` to inject a
    pre-seeded fake snapshot without poking the module-level singleton.
    """
    return get_snapshot()


@router.get("/summary")
async def account_summary(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    snapshot: IBAccountSnapshot = Depends(_get_snapshot_dep),  # noqa: B008
) -> dict[str, float]:
    """IB account summary with key financial metrics.

    SF iter-2 P1: when the snapshot has NEVER successfully refreshed
    (cold-start + IB unreachable), the cached values are the
    ``_ZERO_SUMMARY`` shape — returning them would lie to the dashboard
    by displaying ``$0.00`` indistinguishable from a truly-empty account.
    Raise 503 in that case so TanStack Query routes through the error
    path and the dashboard's per-source error banner fires honestly.
    Once a single refresh has succeeded, we serve cached values across
    transient gateway flaps (last-known-good).
    """
    _ = claims  # auth dependency only — claims are validated in get_current_user
    if snapshot.last_refresh_success_at is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "IB Gateway unreachable — account data unavailable. "
                "Check /api/v1/account/health and the /system page for "
                "subsystem status. Once IB connects, the snapshot "
                "refreshes within 30 seconds."
            ),
        )
    return snapshot.get_summary()


@router.get("/portfolio")
async def account_portfolio(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
    snapshot: IBAccountSnapshot = Depends(_get_snapshot_dep),  # noqa: B008
) -> list[dict[str, Any]]:
    """IB portfolio positions.

    Same cold-start guard as ``/summary`` — empty list at boot before
    any refresh succeeded is indistinguishable from "no positions"; 503
    in that case surfaces the gateway outage honestly.
    """
    _ = claims
    if snapshot.last_refresh_success_at is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "IB Gateway unreachable — portfolio data unavailable. "
                "Check /api/v1/account/health for connection status."
            ),
        )
    return snapshot.get_portfolio()


@router.get("/health")
async def account_health(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> dict[str, str | bool | int]:
    """IB Gateway connection health status.

    iter-5 verify-e2e Issue G (P2): consecutive_failures was returned as
    a string ("1525") which broke numeric comparisons + clients reading
    the OpenAPI spec. Switched to int — matches the underlying
    ``IBProbe.consecutive_failures`` type.
    """
    _ = claims
    return {
        "status": "healthy" if _ib_probe.is_healthy else "unhealthy",
        "gateway_connected": _ib_probe.is_healthy,
        "consecutive_failures": _ib_probe.consecutive_failures,
    }
