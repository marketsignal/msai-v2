from __future__ import annotations

import asyncio
import os
import socket

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.core.queue import get_redis_pool
from msai.services.alerting import alerting_service
from msai.services.compute_slots import (
    ComputeSlotUnavailableError,
    acquire_compute_slots,
    release_compute_slots,
    renew_compute_slots,
)
from msai.services.portfolio_service import PortfolioService

logger = get_logger("workers.portfolio_job")


async def run_portfolio_job(ctx: dict, run_id: str) -> None:
    service = PortfolioService()
    worker_id = str(ctx.get("worker_instance_id") or f"{socket.gethostname()}:{os.getpid()}")
    slot_pool = await get_redis_pool()
    stop_heartbeat = asyncio.Event()
    slot_lease_id: str | None = None
    try:
        run = service.load_run(run_id)
        service.mark_run_running(run_id, worker_id=worker_id)

        async def _heartbeat_loop() -> None:
            while not stop_heartbeat.is_set():
                await asyncio.sleep(settings.portfolio_job_heartbeat_seconds)
                if stop_heartbeat.is_set():
                    return
                if slot_lease_id is not None:
                    await renew_compute_slots(slot_pool, slot_lease_id)
                service.heartbeat_run(run_id, worker_id=worker_id)

        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        requested_slots = max(
            1,
            int(run.get("max_parallelism") or settings.research_max_parallelism),
        )
        slot_lease = await acquire_compute_slots(
            slot_pool,
            job_kind="portfolio",
            job_id=run_id,
            requested_slots=requested_slots,
        )
        slot_lease_id = str(slot_lease["lease_id"])
        await service.run_portfolio_backtest(run_id)
    except ComputeSlotUnavailableError as exc:
        logger.exception("portfolio_slots_unavailable", run_id=run_id, error=str(exc))
        service.mark_run_failed(run_id, error_message=str(exc))
        alerting_service.send_alert(
            "error",
            "Portfolio backtest waiting for compute slots timed out",
            f"run_id={run_id} error={exc}",
        )
    except Exception as exc:
        logger.exception("portfolio_run_failed", run_id=run_id, error=str(exc))
        service.mark_run_failed(run_id, error_message=str(exc))
        alerting_service.send_alert(
            "error",
            "Portfolio backtest failed",
            f"run_id={run_id} error={exc}",
        )
        raise
    finally:
        stop_heartbeat.set()
        heartbeat_task = locals().get("heartbeat_task")
        if heartbeat_task is not None:
            await heartbeat_task
        if slot_lease_id is not None:
            await release_compute_slots(slot_pool, slot_lease_id)
