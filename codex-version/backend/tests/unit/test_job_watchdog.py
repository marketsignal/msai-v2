from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from msai.core.config import settings
from msai.services.portfolio_service import PortfolioService
from msai.services.research_jobs import ResearchJobService
from msai.workers import job_watchdog


@pytest.mark.asyncio
async def test_watchdog_fails_stale_running_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service = ResearchJobService(root=tmp_path / "jobs")
    job = service.create_job(
        job_type="parameter_sweep",
        strategy_id="strategy-1",
        strategy_name="example.mean_reversion",
        strategy_path="/tmp/strategy.py",
        request={"instruments": ["SPY.EQUS"]},
    )
    service.mark_enqueued(job["id"], queue_name=settings.research_queue_name, queue_job_id="arq-job-1")
    service.mark_running(job["id"], worker_id="worker-1")
    path = tmp_path / "jobs" / f"{job['id']}.json"
    payload = json.loads(path.read_text())
    payload["heartbeat_at"] = (datetime.now(UTC) - timedelta(seconds=settings.research_job_stale_seconds + 5)).isoformat()
    path.write_text(json.dumps(payload))

    pool = AsyncMock()
    pool.exists.side_effect = [1, 0]
    monkeypatch.setattr(job_watchdog, "ResearchJobService", lambda: service)
    monkeypatch.setattr(job_watchdog, "get_redis_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(job_watchdog, "_scan_backtest_jobs", AsyncMock())
    monkeypatch.setattr(job_watchdog, "_scan_portfolio_runs", AsyncMock())

    await job_watchdog.run_watchdog_once()

    updated = service.load_job(job["id"])
    assert updated["status"] == "failed"
    pool.zrem.assert_awaited()
    pool.delete.assert_awaited()


@pytest.mark.asyncio
async def test_watchdog_cancels_pending_job_with_cancel_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ResearchJobService(root=tmp_path / "jobs")
    job = service.create_job(
        job_type="walk_forward",
        strategy_id="strategy-1",
        strategy_name="example.mean_reversion",
        strategy_path="/tmp/strategy.py",
        request={"instruments": ["SPY.EQUS"]},
    )
    service.mark_enqueued(job["id"], queue_name=settings.research_queue_name, queue_job_id="arq-job-2")
    service.request_cancel(job["id"])

    pool = AsyncMock()
    monkeypatch.setattr(job_watchdog, "ResearchJobService", lambda: service)
    monkeypatch.setattr(job_watchdog, "get_redis_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(job_watchdog, "_scan_backtest_jobs", AsyncMock())
    monkeypatch.setattr(job_watchdog, "_scan_portfolio_runs", AsyncMock())

    await job_watchdog.run_watchdog_once()

    updated = service.load_job(job["id"])
    assert updated["status"] == "cancelled"
    pool.zrem.assert_awaited()
    pool.delete.assert_awaited()


@pytest.mark.asyncio
async def test_watchdog_fails_stale_backtest_job(monkeypatch: pytest.MonkeyPatch) -> None:
    stale_backtest = SimpleNamespace(
        id="backtest-1",
        status="running",
        queue_name=settings.backtest_queue_name,
        queue_job_id="backtest-1",
        worker_id="worker-1",
        created_at=datetime.now(UTC) - timedelta(minutes=30),
        started_at=datetime.now(UTC) - timedelta(minutes=20),
        heartbeat_at=datetime.now(UTC) - timedelta(seconds=settings.backtest_job_stale_seconds + 5),
    )

    class _FakeResult:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return iter(self._rows)

    class _FakeSession:
        async def execute(self, _query):
            return _FakeResult([stale_backtest])

    class _FakeSessionFactory:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    pool = AsyncMock()
    pool.exists.side_effect = [1, 0]
    mark_failed = AsyncMock()

    monkeypatch.setattr(job_watchdog, "async_session_factory", lambda: _FakeSessionFactory())
    monkeypatch.setattr(job_watchdog, "_mark_backtest_failed", mark_failed)

    await job_watchdog._scan_backtest_jobs(pool)

    mark_failed.assert_awaited_once()
    pool.zrem.assert_awaited()
    pool.delete.assert_awaited()


@pytest.mark.asyncio
async def test_watchdog_fails_stale_portfolio_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PortfolioService(root=tmp_path / "portfolio")
    run = {
        "id": "portfolio-run-1",
        "portfolio_id": "portfolio-1",
        "portfolio_name": "Test",
        "created_by": "user-1",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "max_parallelism": 2,
        "error_message": None,
        "metrics": None,
        "series": [],
        "allocations": [],
        "report_path": None,
        "queue_name": settings.portfolio_queue_name,
        "queue_job_id": "portfolio-run-1",
        "worker_id": "worker-2",
        "attempt": 1,
        "heartbeat_at": (
            datetime.now(UTC) - timedelta(seconds=settings.portfolio_job_stale_seconds + 5)
        ).isoformat(),
    }
    service._write_json(service._run_path(run["id"]), run)
    service.mark_run_enqueued(
        run["id"],
        queue_name=settings.portfolio_queue_name,
        queue_job_id=run["id"],
    )

    pool = AsyncMock()
    pool.exists.side_effect = [1, 0]
    monkeypatch.setattr(job_watchdog, "PortfolioService", lambda: service)

    await job_watchdog._scan_portfolio_runs(pool)

    updated = service.load_run(run["id"])
    assert updated["status"] == "failed"
    pool.zrem.assert_awaited()
    pool.delete.assert_awaited()
