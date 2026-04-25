"""Integration tests for the ``run_symbol_onboarding`` arq worker task.

Council-pinned semantics under test:

* All-success per-symbol -> run-level ``COMPLETED``.
* Any per-symbol failure -> run-level ``COMPLETED_WITH_FAILURES``.
* Run-level ``FAILED`` is reserved for systemic short-circuits ONLY (the
  outer try/except path); per-symbol failures NEVER bubble up.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.schemas.symbol_onboarding import SymbolStateRow, SymbolStatus, SymbolStepStatus
from msai.workers.symbol_onboarding_job import run_symbol_onboarding


def _state(
    status: str,
    step: str,
    error: dict[str, object] | None = None,
) -> SymbolStateRow:
    return SymbolStateRow(
        symbol="SPY",
        asset_class="equity",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        status=SymbolStatus(status),
        step=SymbolStepStatus(step),
        error=error,
    )


@pytest.mark.asyncio
async def test_worker_marks_run_completed_when_every_symbol_succeeds(session_factory, tmp_path):
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.PENDING,
            job_id_digest="test-digest-worker-happy",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "not_started",
                    "step": "pending",
                }
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    fake_state = _state("succeeded", "ib_skipped")
    with (
        patch(
            "msai.workers.symbol_onboarding_job._onboard_one_symbol",
            new=AsyncMock(return_value=fake_state),
        ),
        patch(
            "msai.workers.symbol_onboarding_job.async_session_factory",
            new=session_factory,
        ),
        patch("msai.workers.symbol_onboarding_job.settings") as fake_settings,
    ):
        fake_settings.data_root = str(tmp_path)
        ctx: dict[str, object] = {"redis": AsyncMock()}
        result = await run_symbol_onboarding(ctx, run_id=str(run_id))

    assert result["status"] == "completed"
    async with session_factory() as db:
        persisted = (
            await db.execute(select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id))
        ).scalar_one()
        assert persisted.status == SymbolOnboardingRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_worker_marks_run_completed_with_failures_when_every_symbol_fails(
    session_factory, tmp_path
):
    """Council-pinned: per-symbol failures NEVER bubble to run-level failed."""
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.PENDING,
            job_id_digest="test-digest-worker-allfail",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "not_started",
                    "step": "pending",
                }
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    fake_state = _state(
        "failed",
        "ingest",
        error={"code": "INGEST_FAILED", "message": "x"},
    )
    with (
        patch(
            "msai.workers.symbol_onboarding_job._onboard_one_symbol",
            new=AsyncMock(return_value=fake_state),
        ),
        patch(
            "msai.workers.symbol_onboarding_job.async_session_factory",
            new=session_factory,
        ),
        patch("msai.workers.symbol_onboarding_job.settings") as fake_settings,
    ):
        fake_settings.data_root = str(tmp_path)
        result = await run_symbol_onboarding({"redis": AsyncMock()}, run_id=str(run_id))

    assert result["status"] == "completed_with_failures"
    async with session_factory() as db:
        persisted = (
            await db.execute(select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id))
        ).scalar_one()
        assert persisted.status == SymbolOnboardingRunStatus.COMPLETED_WITH_FAILURES


@pytest.mark.asyncio
async def test_worker_marks_run_failed_on_systemic_short_circuit(session_factory, tmp_path):
    """The ONLY path to run-level FAILED: unhandled exception inside the worker."""
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.PENDING,
            job_id_digest="test-digest-worker-systemic",
            symbol_states={
                "SPY": {
                    "symbol": "SPY",
                    "asset_class": "equity",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "status": "not_started",
                    "step": "pending",
                }
            },
            request_live_qualification=False,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    with (
        patch(
            "msai.workers.symbol_onboarding_job._onboard_one_symbol",
            new=AsyncMock(side_effect=RuntimeError("db connection reset")),
        ),
        patch(
            "msai.workers.symbol_onboarding_job.async_session_factory",
            new=session_factory,
        ),
        patch("msai.workers.symbol_onboarding_job.settings") as fake_settings,
    ):
        fake_settings.data_root = str(tmp_path)
        with pytest.raises(RuntimeError):
            await run_symbol_onboarding({"redis": AsyncMock()}, run_id=str(run_id))

    async with session_factory() as db:
        persisted = (
            await db.execute(select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id))
        ).scalar_one()
        assert persisted.status == SymbolOnboardingRunStatus.FAILED
