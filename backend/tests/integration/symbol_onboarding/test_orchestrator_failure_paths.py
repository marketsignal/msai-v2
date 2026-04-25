"""Integration tests for ``_onboard_one_symbol`` failure paths (T15).

The happy path is pinned in :mod:`test_orchestrator`. This file covers
every per-symbol failure mode the orchestrator persists into the
``symbol_states`` JSONB envelope:

* ``BOOTSTRAP_FAILED`` (service raises)
* ``BOOTSTRAP_AMBIGUOUS`` (ambiguous result outcome)
* ``INGEST_FAILED`` (ingest helper raises)
* ``COVERAGE_INCOMPLETE`` (post-ingest scan reports gaps)
* ``IB_TIMEOUT`` (IB qualify exceeds the SLA — also asserts the
  ``onboarding_ib_timeout_total`` counter increments by exactly 1)
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING
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
from msai.services.observability.trading_metrics import onboarding_ib_timeout_total
from msai.services.symbol_onboarding.orchestrator import _onboard_one_symbol

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _spec() -> OnboardSymbolSpec:
    return OnboardSymbolSpec(
        symbol="SPY",
        asset_class="equity",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
    )


async def _seed_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    request_live_qualification: bool = False,
    digest_suffix: str = "",
) -> UUID:
    """Seed a single-symbol IN_PROGRESS run; return its id."""
    async with session_factory() as db:
        run = SymbolOnboardingRun(
            id=uuid4(),
            watchlist_name="t",
            status=SymbolOnboardingRunStatus.IN_PROGRESS,
            job_id_digest=f"test-digest-{digest_suffix}-{uuid4().hex[:8]}",
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
            request_live_qualification=request_live_qualification,
        )
        db.add(run)
        await db.commit()
        return run.id


def _seed_full_year_parquet(tmp_path: Path) -> None:
    """Create the canonical 12-month Parquet layout the coverage scan expects."""
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    base.mkdir(parents=True, exist_ok=True)
    for month in range(1, 13):
        (base / f"{month:02d}.parquet").write_bytes(b"")


def _ok_bootstrap_mock() -> AsyncMock:
    fake = AsyncMock()
    fake.bootstrap = AsyncMock(
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
    return fake


def _ok_ingest_result() -> IngestResult:
    return IngestResult(bars_written=258_000, symbols_covered=["SPY"], empty_symbols=[])


# ---------------------------------------------------------------------------
# Phase-1 (bootstrap) failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_failed_when_service_raises(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Service-level exception in bootstrap -> BOOTSTRAP_FAILED envelope."""
    run_id = await _seed_run(session_factory, digest_suffix="boot-raises")

    fake_bootstrap = AsyncMock()
    fake_bootstrap.bootstrap = AsyncMock(side_effect=RuntimeError("databento auth failed"))

    state = await _onboard_one_symbol(
        run_id=run_id,
        spec=_spec(),
        request_live_qualification=False,
        db_factory=session_factory,
        data_root=tmp_path,
        bootstrap_service=fake_bootstrap,
    )

    assert state.status is SymbolStatus.FAILED
    assert state.step is SymbolStepStatus.BOOTSTRAP
    assert state.error is not None
    assert state.error["code"] == "BOOTSTRAP_FAILED"
    assert "databento auth failed" in state.error["message"]


@pytest.mark.asyncio
async def test_bootstrap_ambiguous_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Ambiguous bootstrap outcome -> BOOTSTRAP_AMBIGUOUS envelope."""
    run_id = await _seed_run(session_factory, digest_suffix="boot-ambig")

    fake_bootstrap = AsyncMock()
    fake_bootstrap.bootstrap = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="SPY",
                outcome="ambiguous",
                registered=False,
                backtest_data_available=False,
                live_qualified=False,
            )
        ]
    )

    state = await _onboard_one_symbol(
        run_id=run_id,
        spec=_spec(),
        request_live_qualification=False,
        db_factory=session_factory,
        data_root=tmp_path,
        bootstrap_service=fake_bootstrap,
    )

    assert state.status is SymbolStatus.FAILED
    assert state.step is SymbolStepStatus.BOOTSTRAP
    assert state.error is not None
    assert state.error["code"] == "BOOTSTRAP_AMBIGUOUS"


# ---------------------------------------------------------------------------
# Phase-2 (ingest) failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_failed_when_helper_raises(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """`ingest_symbols` raising -> INGEST_FAILED envelope on the persistent state."""
    run_id = await _seed_run(session_factory, digest_suffix="ingest-raises")

    fake_bootstrap = _ok_bootstrap_mock()
    fake_ingest = AsyncMock(side_effect=RuntimeError("databento outage"))

    with patch(
        "msai.services.symbol_onboarding.orchestrator.ingest_symbols",
        new=fake_ingest,
    ):
        state = await _onboard_one_symbol(
            run_id=run_id,
            spec=_spec(),
            request_live_qualification=False,
            db_factory=session_factory,
            data_root=tmp_path,
            bootstrap_service=fake_bootstrap,
        )

    assert state.status is SymbolStatus.FAILED
    assert state.step is SymbolStepStatus.INGEST
    assert state.error is not None
    assert state.error["code"] == "INGEST_FAILED"
    assert "databento outage" in state.error["message"]


# ---------------------------------------------------------------------------
# Phase-3 (coverage) failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_incomplete_when_parquet_missing_month(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Bootstrap + ingest succeed but only 11/12 months on disk -> COVERAGE_INCOMPLETE."""
    run_id = await _seed_run(session_factory, digest_suffix="coverage-gap")

    # Seed Jan..Nov; deliberately omit December so the post-ingest scan finds a gap.
    base = tmp_path / "parquet" / "stocks" / "SPY" / "2024"
    base.mkdir(parents=True, exist_ok=True)
    for month in range(1, 12):  # 1..11 inclusive
        (base / f"{month:02d}.parquet").write_bytes(b"")

    fake_bootstrap = _ok_bootstrap_mock()
    fake_ingest = AsyncMock(return_value=_ok_ingest_result())

    with patch(
        "msai.services.symbol_onboarding.orchestrator.ingest_symbols",
        new=fake_ingest,
    ):
        state = await _onboard_one_symbol(
            run_id=run_id,
            spec=_spec(),
            request_live_qualification=False,
            db_factory=session_factory,
            data_root=tmp_path,
            bootstrap_service=fake_bootstrap,
            # Pin ``today`` past the coverage window so the trailing-edge
            # tolerance doesn't mask the missing December.
            today=date(2025, 6, 1),
        )

    assert state.status is SymbolStatus.FAILED
    assert state.step is SymbolStepStatus.COVERAGE_FAILED
    assert state.error is not None
    assert state.error["code"] == "COVERAGE_INCOMPLETE"
    details = state.error.get("details")
    assert details is not None, "COVERAGE_INCOMPLETE must surface details for operator triage"
    missing = details["missing_ranges"]
    assert isinstance(missing, list) and len(missing) >= 1


# ---------------------------------------------------------------------------
# Phase-4 (IB qualification) failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ib_timeout_increments_metric(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """IB qualify hanging past ib_timeout_s -> IB_TIMEOUT envelope + metric increment."""
    run_id = await _seed_run(
        session_factory,
        request_live_qualification=True,
        digest_suffix="ib-timeout",
    )
    _seed_full_year_parquet(tmp_path)

    # Snapshot the unlabeled counter before invoking the orchestrator so we
    # can assert an exact +1 increment regardless of cross-test pollution.
    before = onboarding_ib_timeout_total._values.get((), 0.0)

    class _HangingIB:
        async def qualify(self, *, symbol: str, asset_class: str) -> None:
            # Sleep well past the orchestrator's ib_timeout_s; asyncio.wait_for
            # cancels this coroutine before it returns.
            await asyncio.sleep(5.0)

    fake_bootstrap = _ok_bootstrap_mock()
    fake_ingest = AsyncMock(return_value=_ok_ingest_result())

    with patch(
        "msai.services.symbol_onboarding.orchestrator.ingest_symbols",
        new=fake_ingest,
    ):
        state = await _onboard_one_symbol(
            run_id=run_id,
            spec=_spec(),
            request_live_qualification=True,
            db_factory=session_factory,
            data_root=tmp_path,
            bootstrap_service=fake_bootstrap,
            ib_service=_HangingIB(),
            ib_timeout_s=1,  # smallest int the timeout helper accepts
        )

    after = onboarding_ib_timeout_total._values.get((), 0.0)

    assert state.status is SymbolStatus.FAILED
    assert state.step is SymbolStepStatus.IB_QUALIFY
    assert state.error is not None
    assert state.error["code"] == "IB_TIMEOUT"
    assert after == before + 1.0, (
        f"onboarding_ib_timeout_total expected +1 increment (before={before}, after={after})"
    )
