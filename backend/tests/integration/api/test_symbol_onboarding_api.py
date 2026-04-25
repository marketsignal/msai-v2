"""Integration tests for POST /onboard, GET /status, POST /repair.

Verifies the council-pinned idempotency contract:
- enqueue-first-then-commit
- duplicate by digest -> 200 OK + existing run_id
- enqueue race (job is None) without committed row -> 409 DUPLICATE_IN_FLIGHT
- pool / enqueue raises -> 503 QUEUE_UNAVAILABLE, no row written

Uses ``httpx.AsyncClient`` + ``ASGITransport`` (NOT TestClient) so the
async DB engine and the app share the same event loop — TestClient
spawns a worker thread which causes "future attached to a different
loop" errors against an asyncpg engine created in the test's loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

from msai.api.symbol_onboarding import router as symbol_onboarding_router
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _build_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()

    async def _stub_user() -> dict[str, str]:
        return {"sub": "test-user", "email": "test@example.com"}

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_current_user] = _stub_user
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(symbol_onboarding_router)
    return app


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = _build_app(session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


_DEFAULT_JOB = object()  # sentinel — distinct from None so callers can request a None return.


def _make_pool(
    *, enqueue_returns: Any = _DEFAULT_JOB, raises: Exception | None = None
) -> MagicMock:
    pool = MagicMock()
    if raises is not None:
        pool.enqueue_job = AsyncMock(side_effect=raises)
    else:
        if enqueue_returns is _DEFAULT_JOB:
            job_obj = MagicMock()
            job_obj.job_id = "fake-job-id"
            return_value = job_obj
        else:
            return_value = enqueue_returns
        pool.enqueue_job = AsyncMock(return_value=return_value)
    pool.abort_job = AsyncMock(return_value=None)
    return pool


def _body(symbol: str = "SPY") -> dict[str, Any]:
    return {
        "watchlist_name": "core",
        "symbols": [
            {
                "symbol": symbol,
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
        ],
        "request_live_qualification": False,
    }


@pytest.mark.asyncio
async def test_post_onboard_returns_202_and_enqueues_task(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pool = _make_pool()
    with patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/api/v1/symbols/onboard", json=_body())

    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["watchlist_name"] == "core"
    assert "run_id" in data

    pool.enqueue_job.assert_awaited_once()
    call = pool.enqueue_job.await_args
    assert call.args[0] == "run_symbol_onboarding"
    assert call.kwargs["_queue_name"] == "msai:ingest"
    assert call.kwargs["_job_id"].startswith("symbol-onboarding:")

    async with session_factory() as s:
        rows = (await s.execute(select(SymbolOnboardingRun))).scalars().all()
    assert len(rows) == 1
    assert str(rows[0].id) == data["run_id"]


@pytest.mark.asyncio
async def test_duplicate_submit_returns_200_with_existing_run_id(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pool = _make_pool()
    with patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)):
        first = await client.post("/api/v1/symbols/onboard", json=_body())
        assert first.status_code == 202
        second = await client.post("/api/v1/symbols/onboard", json=_body())

    assert second.status_code == 200, second.text
    assert second.json()["run_id"] == first.json()["run_id"]
    pool.enqueue_job.assert_awaited_once()

    async with session_factory() as s:
        rows = (await s.execute(select(SymbolOnboardingRun))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_duplicate_submit_during_race_returns_409_when_row_not_visible_yet(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``enqueue_job`` returns ``None`` (arq dedup) and no committed row materialized."""
    pool = _make_pool(enqueue_returns=None)
    with patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/api/v1/symbols/onboard", json=_body())

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "DUPLICATE_IN_FLIGHT"

    async with session_factory() as s:
        rows = (await s.execute(select(SymbolOnboardingRun))).scalars().all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_redis_down_returns_503_and_commits_no_row(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pool = _make_pool(raises=ConnectionError("redis down"))
    with patch("msai.api.symbol_onboarding._get_arq_pool", new=AsyncMock(return_value=pool)):
        resp = await client.post("/api/v1/symbols/onboard", json=_body())

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "QUEUE_UNAVAILABLE"

    async with session_factory() as s:
        rows = (await s.execute(select(SymbolOnboardingRun))).scalars().all()
    assert len(rows) == 0


@pytest_asyncio.fixture
async def seeded_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> SymbolOnboardingRun:
    """Seed a SymbolOnboardingRun with mixed per-symbol states."""
    run = SymbolOnboardingRun(
        id=uuid4(),
        watchlist_name="core",
        status=SymbolOnboardingRunStatus.COMPLETED_WITH_FAILURES,
        symbol_states={
            "AAPL": {
                "symbol": "AAPL",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "succeeded",
                "step": "completed",
                "error": None,
            },
            "BAD": {
                "symbol": "BAD",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "failed",
                "step": "bootstrap",
                "error": {"code": "BOOTSTRAP_AMBIGUOUS", "message": "ambiguous"},
            },
            "ZIN": {
                "symbol": "ZIN",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "in_progress",
                "step": "ingest",
                "error": None,
            },
            "WAITING": {
                "symbol": "WAITING",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "not_started",
                "step": "pending",
                "error": None,
            },
        },
        request_live_qualification=False,
        cost_ceiling_usd=None,
        job_id_digest="seeded-digest-test",
    )
    async with session_factory() as s:
        s.add(run)
        await s.commit()
        await s.refresh(run)
    return run


@pytest.mark.asyncio
async def test_get_status_returns_progress_counts(
    client: httpx.AsyncClient,
    seeded_run: SymbolOnboardingRun,
) -> None:
    resp = await client.get(f"/api/v1/symbols/onboard/{seeded_run.id}/status")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed_with_failures"
    progress = data["progress"]
    assert progress == {
        "total": 4,
        "succeeded": 1,
        "failed": 1,
        "in_progress": 1,
        "not_started": 1,
    }
    by_symbol = {row["symbol"]: row for row in data["per_symbol"]}
    assert by_symbol["BAD"]["next_action"] is not None
    assert by_symbol["AAPL"]["next_action"] is None


@pytest.mark.asyncio
async def test_get_status_404_for_unknown_run(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get(f"/api/v1/symbols/onboard/{uuid4()}/status")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_post_repair_rejects_in_progress_parent(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    parent = SymbolOnboardingRun(
        id=uuid4(),
        watchlist_name="core",
        status=SymbolOnboardingRunStatus.IN_PROGRESS,
        symbol_states={
            "X": {
                "symbol": "X",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
                "status": "failed",
                "step": "bootstrap",
                "error": {"code": "INGEST_FAILED", "message": "x"},
            }
        },
        request_live_qualification=False,
        cost_ceiling_usd=None,
        job_id_digest="parent-in-progress",
    )
    async with session_factory() as s:
        s.add(parent)
        await s.commit()
        await s.refresh(parent)

    resp = await client.post(f"/api/v1/symbols/onboard/{parent.id}/repair", json={})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "PARENT_RUN_IN_PROGRESS"
