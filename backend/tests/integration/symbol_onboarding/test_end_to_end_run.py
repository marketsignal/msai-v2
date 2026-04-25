"""End-to-end run test for symbol onboarding (T15).

Exercises the full POST -> worker -> terminal pipeline by invoking the
``run_symbol_onboarding`` arq entrypoint with the real orchestrator
plumbed end-to-end against a testcontainers Postgres. The Databento
bootstrap service and ``ingest_symbols`` helper are patched at the leaf
boundary so the test is hermetic, but every other stage (the worker's
status sync, ``_onboard_one_symbol`` four-phase pipeline, JSONB writes,
coverage scan against the on-disk Parquet layout, and metrics emission)
runs unmocked.

This is the test that pins the wave's contract: a happy-path run lands
on ``COMPLETED`` with the per-symbol terminal envelope persisted under
``ib_skipped`` when ``request_live_qualification=False``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.services.data_ingestion import IngestResult
from msai.workers.symbol_onboarding_job import run_symbol_onboarding

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.asyncio
async def test_end_to_end_run_marks_completed_with_persisted_envelope(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Full pipeline: PENDING run -> worker invocation -> COMPLETED + ib_skipped envelope."""
    # ---- Arrange ----
    # Seed a single-symbol PENDING run; the worker flips it to IN_PROGRESS.
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.PENDING,
            job_id_digest=f"e2e-{uuid4().hex[:8]}",
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

    # Seed full-year Parquet so the post-ingest coverage scan reports "full".
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    base.mkdir(parents=True, exist_ok=True)
    for month in range(1, 13):
        (base / f"{month:02d}.parquet").write_bytes(b"")

    # Patch the LEAF services the orchestrator depends on, not
    # ``_onboard_one_symbol`` itself, so the four-phase pipeline runs in
    # full and we exercise the JSONB persistence + status sync paths
    # against the real testcontainers Postgres.
    fake_bootstrap_service = AsyncMock()
    fake_bootstrap_service.bootstrap = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="SPY",
                outcome="created",
                registered=True,
                backtest_data_available=False,
                live_qualified=False,
            )
        ]
    )

    fake_ingest = AsyncMock(
        return_value=IngestResult(
            bars_written=258_000,
            symbols_covered=["SPY"],
            empty_symbols=[],
        )
    )

    # ---- Act ----
    with (
        patch(
            "msai.workers.symbol_onboarding_job.async_session_factory",
            new=session_factory,
        ),
        patch("msai.workers.symbol_onboarding_job.settings") as fake_settings,
        # Patch ``_default_bootstrap_service`` (called when the
        # orchestrator is invoked without ``bootstrap_service=``); the
        # worker doesn't pass one, so we intercept here.
        patch(
            "msai.services.symbol_onboarding.orchestrator._default_bootstrap_service",
            return_value=fake_bootstrap_service,
        ),
        patch(
            "msai.services.symbol_onboarding.orchestrator.ingest_symbols",
            new=fake_ingest,
        ),
    ):
        fake_settings.data_root = str(tmp_path)
        result = await run_symbol_onboarding({"redis": AsyncMock()}, run_id=str(run_id))

    # ---- Assert ----
    assert result["status"] == "completed"
    assert result["run_id"] == str(run_id)

    async with session_factory() as db:
        persisted = (
            await db.execute(select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id))
        ).scalar_one()

        # Run-level lifecycle: started_at + completed_at populated, terminal status COMPLETED.
        assert persisted.status == SymbolOnboardingRunStatus.COMPLETED
        assert persisted.started_at is not None, "worker must stamp started_at on Phase A entry"
        assert persisted.completed_at is not None, "worker must stamp completed_at on terminal sync"

        # Per-symbol envelope: succeeded + ib_skipped (request_live_qualification=False).
        spy_state = persisted.symbol_states["SPY"]
        assert spy_state["status"] == "succeeded"
        assert spy_state["step"] == "ib_skipped"
        assert spy_state["error"] is None
        assert spy_state["symbol"] == "SPY"

    # Sanity: the leaf services were exercised (proves the orchestrator wasn't
    # short-circuited by an ARRANGE bug).
    fake_bootstrap_service.bootstrap.assert_awaited_once()
    fake_ingest.assert_awaited_once()
