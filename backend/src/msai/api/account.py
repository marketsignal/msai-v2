"""Account API router -- IB account summary, portfolio, and health.

Provides endpoints to query Interactive Brokers account data and
gateway connectivity status.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Depends

from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.logging import get_logger
from msai.services.ib_account import IBAccountService
from msai.services.ib_probe import IBProbe

log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/account", tags=["account"])

# Module-level singletons (per process). The probe is created here
# but its periodic loop is started by ``start_ib_probe_task`` from
# the FastAPI lifespan in ``main.py`` — without that the probe
# never runs ``check_health`` and the endpoint always reports
# ``gateway_connected=false`` regardless of actual IB state. Drill
# 2026-04-15 misled me three times because of this.
_ib_service = IBAccountService(host=settings.ib_host, port=settings.ib_port)
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
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _probe_task
    _probe_task = None

    _ib_probe._is_healthy = False  # noqa: SLF001
    _ib_probe._consecutive_failures = 0  # noqa: SLF001

    if task_was_running:
        log.info("ib_probe_task_stopped")


@router.get("/summary")
async def account_summary(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> dict[str, float]:
    """IB account summary with key financial metrics."""
    return await _ib_service.get_summary()


@router.get("/portfolio")
async def account_portfolio(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> list[dict[str, Any]]:
    """IB portfolio positions."""
    return await _ib_service.get_portfolio()


@router.get("/health")
async def account_health(
    claims: dict[str, Any] = Depends(get_current_user),  # noqa: B008
) -> dict[str, str | bool]:
    """IB Gateway connection health status."""
    return {
        "status": "healthy" if _ib_probe.is_healthy else "unhealthy",
        "gateway_connected": _ib_probe.is_healthy,
        "consecutive_failures": str(_ib_probe.consecutive_failures),
    }
