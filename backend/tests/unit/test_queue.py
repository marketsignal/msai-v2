"""Unit tests for msai.core.queue — URL parsing and job enqueuing."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from arq.connections import ArqRedis, RedisSettings

from msai.core.config import settings
from msai.core.queue import (
    _parse_redis_url,
    enqueue_backtest,
    enqueue_ingest,
)

# ---------------------------------------------------------------------------
# _parse_redis_url
# ---------------------------------------------------------------------------


class TestParseRedisUrl:
    """Tests for ``_parse_redis_url``."""

    def test_parse_redis_url_default(self) -> None:
        """Standard ``redis://localhost:6379`` is parsed correctly."""
        result: RedisSettings = _parse_redis_url("redis://localhost:6379")
        assert result.host == "localhost"
        assert result.port == 6379

    def test_parse_redis_url_custom(self) -> None:
        """Custom host and port are extracted."""
        result: RedisSettings = _parse_redis_url("redis://myhost:7777")
        assert result.host == "myhost"
        assert result.port == 7777

    def test_parse_redis_url_no_port(self) -> None:
        """When port is omitted, defaults to 6379."""
        result: RedisSettings = _parse_redis_url("redis://myhost")
        assert result.host == "myhost"
        assert result.port == 6379


# ---------------------------------------------------------------------------
# enqueue helpers
# ---------------------------------------------------------------------------


class TestEnqueueBacktest:
    """Tests for ``enqueue_backtest``."""

    @pytest.mark.asyncio
    async def test_enqueue_backtest_calls_enqueue_job(self) -> None:
        """``enqueue_backtest`` delegates to ``pool.enqueue_job`` with the right args."""
        # Arrange
        pool = AsyncMock(spec=ArqRedis)
        backtest_id = "bt-001"
        strategy_path = "strategies.momentum.MomentumStrategy"
        config: dict[str, Any] = {"lookback": 20, "threshold": 0.05}

        # Act
        await enqueue_backtest(pool, backtest_id, strategy_path, config)

        # Assert
        pool.enqueue_job.assert_awaited_once_with(
            "run_backtest",
            backtest_id=backtest_id,
            strategy_path=strategy_path,
            config=config,
        )


class TestEnqueueIngest:
    """Tests for ``enqueue_ingest``."""

    @pytest.mark.asyncio
    async def test_enqueue_ingest_calls_enqueue_job(self) -> None:
        """``enqueue_ingest`` delegates to ``pool.enqueue_job`` with the right args."""
        # Arrange
        pool = AsyncMock(spec=ArqRedis)
        asset_class = "equity"
        symbols = ["AAPL", "MSFT"]
        start = "2024-01-01"
        end = "2024-12-31"

        # Act
        await enqueue_ingest(pool, asset_class, symbols, start, end)

        # Assert
        pool.enqueue_job.assert_awaited_once_with(
            "run_ingest",
            asset_class=asset_class,
            symbols=symbols,
            start=start,
            end=end,
            provider="auto",
            dataset=None,
            schema=None,
            _queue_name=settings.ingest_queue_name,
        )

    @pytest.mark.asyncio
    async def test_enqueue_ingest_with_provider_params(self) -> None:
        """``enqueue_ingest`` forwards provider, dataset, and schema kwargs."""
        # Arrange
        pool = AsyncMock(spec=ArqRedis)

        # Act
        await enqueue_ingest(
            pool,
            "stocks",
            ["AAPL"],
            "2024-01-01",
            "2024-12-31",
            provider="databento",
            dataset="EQUS.MINI",
            schema="ohlcv-1m",
        )

        # Assert
        pool.enqueue_job.assert_awaited_once_with(
            "run_ingest",
            asset_class="stocks",
            symbols=["AAPL"],
            start="2024-01-01",
            end="2024-12-31",
            provider="databento",
            dataset="EQUS.MINI",
            schema="ohlcv-1m",
            _queue_name=settings.ingest_queue_name,
        )

    @pytest.mark.asyncio
    async def test_enqueue_ingest_routes_to_ingest_queue(self) -> None:
        """On-demand ingest must NOT land on the default backtest queue."""
        # Arrange
        fake_pool = AsyncMock(spec=ArqRedis)
        fake_pool.enqueue_job = AsyncMock()

        # Act
        await enqueue_ingest(
            pool=fake_pool,
            asset_class="stocks",
            symbols=["AAPL"],
            start="2024-01-01",
            end="2024-12-31",
        )

        # Assert
        assert fake_pool.enqueue_job.await_count == 1
        _, kwargs = fake_pool.enqueue_job.call_args
        assert kwargs.get("_queue_name") == settings.ingest_queue_name


class TestIngestWorkerFunctionRegistration:
    """Task B2 — on-demand ingest must be serviced by the ingest worker."""

    def test_ingest_worker_registers_on_demand_ingest(self) -> None:
        """``IngestWorkerSettings.functions`` must include ``run_ingest``.

        Without this registration the dedicated ``msai:ingest`` queue has no
        consumer for on-demand auto-heal ingest jobs — the `_queue_name`
        routing in ``enqueue_ingest`` would silently route to a dead queue.
        """
        from msai.workers.ingest_settings import IngestWorkerSettings

        fn_names = [fn.__name__ for fn in IngestWorkerSettings.functions]
        assert "run_ingest" in fn_names
        assert "run_nightly_ingest" in fn_names
