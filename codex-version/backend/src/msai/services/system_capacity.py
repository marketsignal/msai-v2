from __future__ import annotations

from arq import ArqRedis

from msai.core.config import settings
from msai.services.compute_slots import describe_compute_slots
from msai.services.worker_registry import summarize_workers


async def describe_system_capacity(pool: ArqRedis) -> dict[str, object]:
    worker_summary = await summarize_workers(
        pool,
        queue_names=[
            settings.ingest_queue_name,
            settings.backtest_queue_name,
            settings.research_queue_name,
            settings.portfolio_queue_name,
            settings.live_runtime_queue_name,
        ],
    )
    return {
        "compute_slots": await describe_compute_slots(pool),
        "workers": worker_summary,
    }
