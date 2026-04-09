from unittest.mock import AsyncMock

import pytest

from msai.core.config import settings
from msai.core.queue import (
    enqueue_backtest,
    enqueue_ingestion,
    enqueue_live_runtime,
    enqueue_research_job,
)


@pytest.mark.asyncio
async def test_enqueue_backtest() -> None:
    pool = AsyncMock()
    await enqueue_backtest(pool, "b1", "strategies/example/ema_cross.py", {"fast": 10})
    pool.enqueue_job.assert_awaited_once_with(
        "run_backtest",
        _job_id="b1",
        _queue_name=settings.backtest_queue_name,
        backtest_id="b1",
        strategy_path="strategies/example/ema_cross.py",
        config={"fast": 10},
    )


@pytest.mark.asyncio
async def test_enqueue_ingestion_includes_provider_metadata() -> None:
    pool = AsyncMock()
    await enqueue_ingestion(
        pool,
        "equities",
        ["AAPL"],
        "2024-01-01",
        "2024-01-02",
        provider="databento",
        dataset="EQUS.MINI",
        schema="ohlcv-1m",
    )
    pool.enqueue_job.assert_awaited_once_with(
        "run_ingest",
        _queue_name=settings.ingest_queue_name,
        asset_class="equities",
        symbols=["AAPL"],
        start="2024-01-01",
        end="2024-01-02",
        provider="databento",
        dataset="EQUS.MINI",
        schema="ohlcv-1m",
    )


@pytest.mark.asyncio
async def test_enqueue_live_runtime_uses_dedicated_queue() -> None:
    pool = AsyncMock()
    await enqueue_live_runtime(pool, "run_live_status")
    pool.enqueue_job.assert_awaited_once_with(
        "run_live_status",
        _queue_name=settings.live_runtime_queue_name,
    )


@pytest.mark.asyncio
async def test_enqueue_research_job_uses_research_queue() -> None:
    pool = AsyncMock()
    await enqueue_research_job(
        pool,
        "job-1",
        "parameter_sweep",
        {"strategy_path": "/tmp/strategy.py", "instruments": ["SPY.EQUS"]},
    )
    pool.enqueue_job.assert_awaited_once_with(
        "run_research_job",
        _queue_name=settings.research_queue_name,
        job_id="job-1",
        job_type="parameter_sweep",
        payload={"strategy_path": "/tmp/strategy.py", "instruments": ["SPY.EQUS"]},
    )
