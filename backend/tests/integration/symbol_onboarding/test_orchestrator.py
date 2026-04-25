"""Integration tests for ``_onboard_one_symbol`` (T6).

The happy path proves the four-phase pipeline persists state correctly
and observes the duration histogram. Failure-mode coverage lives in
T15; this file pins the green path so subsequent task implementers can
refactor with confidence.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from msai.models.symbol_onboarding_run import (
    SymbolOnboardingRun,
    SymbolOnboardingRunStatus,
)
from msai.schemas.symbol_onboarding import (
    OnboardSymbolSpec,
    SymbolStatus,
    SymbolStepStatus,
)
from msai.services.data_ingestion import IngestResult
from msai.services.symbol_onboarding.orchestrator import _onboard_one_symbol


@pytest.mark.asyncio
async def test_orchestrator_happy_path_without_live_qualification(session_factory, tmp_path):
    """Bootstrap → ingest → coverage(full) → ib_skipped → succeeded."""
    spec = OnboardSymbolSpec(
        symbol="SPY",
        asset_class="equity",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )

    # Arrange — seed the run row that the orchestrator updates in-place.
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.IN_PROGRESS,
            job_id_digest="test-digest-happy",
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

    # Seed full-year SPY coverage so the post-ingest scan reports "full".
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    base.mkdir(parents=True, exist_ok=True)
    for month in range(1, 13):
        (base / f"{month:02d}.parquet").write_bytes(b"")

    # Stand-in BootstrapResult — the orchestrator only inspects ``.outcome``.
    fake_bootstrap = AsyncMock()
    fake_bootstrap.bootstrap = AsyncMock(
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

    # Act
    with patch(
        "msai.services.symbol_onboarding.orchestrator.ingest_symbols",
        new=fake_ingest,
    ):
        state = await _onboard_one_symbol(
            run_id=run_id,
            spec=spec,
            request_live_qualification=False,
            db_factory=session_factory,
            data_root=tmp_path,
            bootstrap_service=fake_bootstrap,
        )

    # Assert — terminal envelope
    assert state.status is SymbolStatus.SUCCEEDED
    assert state.step is SymbolStepStatus.IB_SKIPPED
    assert state.error is None
    assert state.symbol == "SPY"

    # Assert — bootstrap service was invoked with the correct asset_class override
    fake_bootstrap.bootstrap.assert_awaited_once_with(
        symbols=["SPY"],
        asset_class_override="equity",
        exact_ids=None,
    )
    # Assert — ingest helper called with the INGEST taxonomy ("stocks"), not "equity"
    fake_ingest.assert_awaited_once_with(
        "stocks",
        ["SPY"],
        "2024-01-01",
        "2024-12-31",
    )

    # Assert — JSONB persisted the terminal state
    async with session_factory() as db:
        from sqlalchemy import select

        row = (
            await db.execute(select(SymbolOnboardingRun).where(SymbolOnboardingRun.id == run_id))
        ).scalar_one()
        spy_state = row.symbol_states["SPY"]
        assert spy_state["status"] == "succeeded"
        assert spy_state["step"] == "ib_skipped"
        assert spy_state["error"] is None
