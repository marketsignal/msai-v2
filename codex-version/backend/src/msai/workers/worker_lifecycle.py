from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from msai.core.config import settings
from msai.services.worker_registry import (
    deregister_worker,
    heartbeat_worker,
    register_worker,
)


async def worker_startup(ctx: dict) -> None:
    redis = ctx["redis"]
    registration = await register_worker(
        redis,
        worker_role=str(ctx.get("worker_role") or "worker"),
        queue_name=str(ctx.get("queue_name") or ""),
        max_jobs=int(ctx.get("max_jobs") or 1),
    )
    ctx["worker_instance_id"] = str(registration["worker_id"])
    ctx["worker_registration"] = registration
    ctx["worker_registry_task"] = asyncio.create_task(_heartbeat_loop(ctx))


async def worker_shutdown(ctx: dict) -> None:
    task = ctx.get("worker_registry_task")
    if isinstance(task, asyncio.Task):
        task.cancel()
        await _await_task(task)
    worker_id = str(ctx.get("worker_instance_id") or "")
    if worker_id:
        await deregister_worker(ctx["redis"], worker_id)


async def _heartbeat_loop(ctx: dict) -> None:
    redis = ctx["redis"]
    worker_id = str(ctx.get("worker_instance_id") or "")
    try:
        while True:
            await asyncio.sleep(max(1, int(settings.worker_registry_heartbeat_seconds)))
            await heartbeat_worker(redis, worker_id)
    except asyncio.CancelledError:
        raise


async def _await_task(task: Awaitable[object]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return
