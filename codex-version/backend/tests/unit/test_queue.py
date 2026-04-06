from unittest.mock import AsyncMock

import pytest

from msai.core.queue import enqueue_backtest


@pytest.mark.asyncio
async def test_enqueue_backtest() -> None:
    pool = AsyncMock()
    await enqueue_backtest(pool, "b1", "strategies/example/ema_cross.py", {"fast": 10})
    pool.enqueue_job.assert_awaited_once()
