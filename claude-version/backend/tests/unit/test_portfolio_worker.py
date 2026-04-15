"""Unit tests for the portfolio-backtest arq worker.

Covers the control-flow wrapper around
:meth:`PortfolioService.run_portfolio_backtest`:

* terminal-state short-circuit (arq retry of a completed/failed run)
* missing run_id at start (arq-picks-a-phantom-job)
* Redis outage before slot acquisition (must mark failed + re-raise)
* `ComputeSlotUnavailableError` (mark failed, no re-raise)
* `PortfolioOrchestrationError` mid-run (mark failed, no re-raise)
* Generic infrastructure error mid-run (mark failed + re-raise)
* Lease release in the ``finally`` block under every path
* Lease renewal task lifetime
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.services.compute_slots import ComputeSlotUnavailableError
from msai.services.portfolio_service import (
    PortfolioOrchestrationError,
    PortfolioRunTerminalStateError,
)
from msai.workers import portfolio_job


@pytest.fixture
def mock_run_id() -> str:
    return "12345678-1234-5678-1234-567812345678"


@pytest.fixture
def mock_portfolio_id() -> str:
    return "87654321-4321-8765-4321-876543218765"


@pytest.fixture
def mock_service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch :class:`PortfolioService` so the worker uses a stub throughout."""
    service = MagicMock()
    service.mark_run_running = AsyncMock()
    service.mark_run_failed = AsyncMock()
    service.run_portfolio_backtest = AsyncMock()
    service.get_run = AsyncMock(return_value=MagicMock(max_parallelism=1, portfolio_id="pid"))
    # Two allocations — enough to verify slot-reservation math.
    service.get_allocations = AsyncMock(return_value=[MagicMock(), MagicMock()])
    monkeypatch.setattr(portfolio_job, "PortfolioService", lambda: service)
    return service


@pytest.fixture
def mock_compute_slots(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch compute_slots helpers so no real Redis traffic is attempted."""
    calls: dict = {"acquire": [], "release": [], "renew": []}

    async def _acquire(*args, **kwargs):
        calls["acquire"].append(kwargs)
        return "test-lease-id"

    async def _release(*args, **kwargs):
        calls["release"].append((args, kwargs))

    async def _renew(*args, **kwargs):
        calls["renew"].append((args, kwargs))

    monkeypatch.setattr(portfolio_job, "acquire_compute_slots", _acquire)
    monkeypatch.setattr(portfolio_job, "release_compute_slots", _release)
    monkeypatch.setattr(portfolio_job, "renew_compute_slots", _renew)
    return calls


@pytest.fixture
def mock_redis_pool(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch :func:`get_redis_pool` to return a bare MagicMock."""
    pool = MagicMock()
    monkeypatch.setattr(
        portfolio_job,
        "get_redis_pool",
        AsyncMock(return_value=pool),
    )
    return pool


@pytest.fixture(autouse=True)
def short_renewal_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink renewal cadence so the cancel path runs promptly in tests."""
    monkeypatch.setattr(portfolio_job, "_RENEWAL_INTERVAL_SECONDS", 3600)


@pytest.fixture
def mock_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Fake session context manager for ``async_session_factory()``."""
    session = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=cm)
    monkeypatch.setattr(portfolio_job, "async_session_factory", factory)
    return session


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_runs_and_releases_slots(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    await portfolio_job.run_portfolio_job({}, mock_run_id, mock_portfolio_id)

    mock_service.mark_run_running.assert_awaited_once()
    mock_service.run_portfolio_backtest.assert_awaited_once()
    mock_service.mark_run_failed.assert_not_called()
    assert len(mock_compute_slots["acquire"]) == 1
    assert len(mock_compute_slots["release"]) == 1  # finally block


async def test_slot_count_clamped_to_allocation_count(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    # Run requests 16x parallelism but only has 2 allocations — worker
    # must reserve min(16, 2, compute_slot_limit) = 2 slots, not hog the
    # cluster semaphore while doing light work.
    mock_service.get_run = AsyncMock(return_value=MagicMock(max_parallelism=16, portfolio_id="pid"))
    mock_service.get_allocations = AsyncMock(return_value=[MagicMock(), MagicMock()])

    await portfolio_job.run_portfolio_job({}, mock_run_id, mock_portfolio_id)

    acquire_call = mock_compute_slots["acquire"][0]
    assert acquire_call["slot_count"] == 2


# ---------------------------------------------------------------------------
# Terminal-state short-circuit
# ---------------------------------------------------------------------------


async def test_terminal_state_short_circuits_without_running(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    mock_service.mark_run_running.side_effect = PortfolioRunTerminalStateError("already completed")

    # Should not raise, should not try to acquire slots or run.
    await portfolio_job.run_portfolio_job({}, mock_run_id, mock_portfolio_id)

    mock_service.run_portfolio_backtest.assert_not_called()
    assert mock_compute_slots["acquire"] == []


async def test_missing_run_reraises_on_non_final_attempt(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-final attempt: re-raise so arq retries (the API enqueue may
    # have raced ahead of the commit; attempt 2 will likely succeed).
    mock_service.mark_run_running.side_effect = PortfolioOrchestrationError("not found")
    monkeypatch.setattr(portfolio_job, "_START_LOOKUP_BACKOFF_SECONDS", 0.0)

    with pytest.raises(PortfolioOrchestrationError):
        await portfolio_job.run_portfolio_job(
            {"job_try": 1, "max_tries": 2}, mock_run_id, mock_portfolio_id
        )

    mock_service.run_portfolio_backtest.assert_not_called()
    mock_service.mark_run_failed.assert_not_called()  # don't lock out retry
    assert mock_compute_slots["acquire"] == []


async def test_missing_run_reraises_on_final_attempt(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Final attempt + still not found → re-raise (not return) so the
    # failure is surfaced in arq's DLQ.  We can't mark the row failed
    # because it still doesn't exist — silently returning would orphan
    # the row once the delayed commit lands.  Known limitation: the
    # row stays ``pending`` until job_watchdog is extended to scan
    # portfolio_runs.
    mock_service.mark_run_running.side_effect = PortfolioOrchestrationError("not found")
    monkeypatch.setattr(portfolio_job, "_START_LOOKUP_BACKOFF_SECONDS", 0.0)

    with pytest.raises(PortfolioOrchestrationError):
        await portfolio_job.run_portfolio_job(
            {"job_try": 2, "max_tries": 2}, mock_run_id, mock_portfolio_id
        )

    mock_service.run_portfolio_backtest.assert_not_called()


async def test_enqueue_before_commit_race_retries_until_row_appears(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    # Simulates the race: API enqueues job BEFORE committing the row,
    # worker fires, first lookup 404s, then the commit lands.
    calls = {"n": 0}

    async def _mark(session, run_uuid):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PortfolioOrchestrationError("not found yet")
        return None

    mock_service.mark_run_running.side_effect = _mark
    monkeypatch.setattr(portfolio_job, "_START_LOOKUP_BACKOFF_SECONDS", 0.0)

    await portfolio_job.run_portfolio_job({}, mock_run_id, mock_portfolio_id)

    # Eventually succeeded — orchestration runs + slots released.
    assert calls["n"] == 3
    mock_service.run_portfolio_backtest.assert_awaited_once()
    assert len(mock_compute_slots["release"]) == 1


# ---------------------------------------------------------------------------
# Redis outage
# ---------------------------------------------------------------------------


async def test_redis_outage_does_not_mark_failed_on_non_final_attempt(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    # Transient Redis outage on attempt 1 → re-raise so arq retries.
    # Marking failed would lock the row out of attempt 2.
    monkeypatch.setattr(
        portfolio_job,
        "get_redis_pool",
        AsyncMock(side_effect=ConnectionError("redis gone")),
    )

    with pytest.raises(ConnectionError):
        await portfolio_job.run_portfolio_job(
            {"job_try": 1, "max_tries": 2}, mock_run_id, mock_portfolio_id
        )

    mock_service.mark_run_running.assert_awaited_once()
    mock_service.mark_run_failed.assert_not_called()


async def test_redis_outage_marks_failed_on_final_attempt(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    # Final attempt → only chance to surface failure to the operator.
    monkeypatch.setattr(
        portfolio_job,
        "get_redis_pool",
        AsyncMock(side_effect=ConnectionError("redis gone")),
    )

    with pytest.raises(ConnectionError):
        await portfolio_job.run_portfolio_job(
            {"job_try": 2, "max_tries": 2}, mock_run_id, mock_portfolio_id
        )

    mock_service.mark_run_failed.assert_awaited_once()
    args, kwargs = mock_service.mark_run_failed.await_args
    assert "Redis unavailable" in kwargs["error_message"]


# ---------------------------------------------------------------------------
# Compute-slot unavailable
# ---------------------------------------------------------------------------


async def test_slots_unavailable_marks_failed_without_raise(
    mock_service: MagicMock,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    async def _acquire_boom(*a, **kw):
        raise ComputeSlotUnavailableError("too busy")

    monkeypatch.setattr(portfolio_job, "acquire_compute_slots", _acquire_boom)
    monkeypatch.setattr(portfolio_job, "release_compute_slots", AsyncMock())

    # Should mark failed and return (not re-raise — arq shouldn't retry).
    await portfolio_job.run_portfolio_job({}, mock_run_id, mock_portfolio_id)

    mock_service.mark_run_failed.assert_awaited_once()
    args, kwargs = mock_service.mark_run_failed.await_args
    assert "Compute slots unavailable" in kwargs["error_message"]
    mock_service.run_portfolio_backtest.assert_not_called()


# ---------------------------------------------------------------------------
# Orchestration errors
# ---------------------------------------------------------------------------


async def test_data_shape_error_marks_failed_without_raise(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    mock_service.run_portfolio_backtest.side_effect = PortfolioOrchestrationError(
        "no instruments configured"
    )

    # Data error — must NOT re-raise so arq won't retry (user-fixable).
    await portfolio_job.run_portfolio_job({}, mock_run_id, mock_portfolio_id)

    mock_service.mark_run_failed.assert_awaited_once()
    _args, kwargs = mock_service.mark_run_failed.await_args
    assert "no instruments configured" in kwargs["error_message"]
    assert len(mock_compute_slots["release"]) == 1


async def test_missing_parquet_data_treated_as_terminal(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    # FileNotFoundError from ensure_catalog_data = missing source data.
    # Deterministic — mark failed immediately, do not re-raise (would
    # just burn an arq retry that can't possibly succeed).
    mock_service.run_portfolio_backtest.side_effect = FileNotFoundError(
        "parquet data for AAPL not found"
    )

    await portfolio_job.run_portfolio_job(
        {"job_try": 1, "max_tries": 2}, mock_run_id, mock_portfolio_id
    )

    mock_service.mark_run_failed.assert_awaited_once()
    _args, kwargs = mock_service.mark_run_failed.await_args
    assert "FileNotFoundError" in kwargs["error_message"]


async def test_backtest_timeout_treated_as_terminal(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    # TimeoutError from BacktestRunner.run = candidate exceeded the
    # configured timeout.  Deterministic — retrying won't help.
    mock_service.run_portfolio_backtest.side_effect = TimeoutError("backtest exceeded 1800s budget")

    await portfolio_job.run_portfolio_job(
        {"job_try": 1, "max_tries": 2}, mock_run_id, mock_portfolio_id
    )

    mock_service.mark_run_failed.assert_awaited_once()
    _args, kwargs = mock_service.mark_run_failed.await_args
    assert "TimeoutError" in kwargs["error_message"]


async def test_infrastructure_error_reraises_without_mark_on_non_final(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    # Infra error on non-final attempt → re-raise, do NOT mark failed
    # (would block arq retry via terminal-state guard).
    mock_service.run_portfolio_backtest.side_effect = RuntimeError("subprocess crashed")

    with pytest.raises(RuntimeError, match="subprocess crashed"):
        await portfolio_job.run_portfolio_job(
            {"job_try": 1, "max_tries": 2}, mock_run_id, mock_portfolio_id
        )

    mock_service.mark_run_failed.assert_not_called()
    assert len(mock_compute_slots["release"]) == 1


async def test_infrastructure_error_marks_failed_on_final_attempt(
    mock_service: MagicMock,
    mock_compute_slots: dict,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    # Final attempt → mark failed + re-raise so arq dead-letters and
    # the operator sees the failure.
    mock_service.run_portfolio_backtest.side_effect = RuntimeError("subprocess crashed")

    with pytest.raises(RuntimeError, match="subprocess crashed"):
        await portfolio_job.run_portfolio_job(
            {"job_try": 2, "max_tries": 2}, mock_run_id, mock_portfolio_id
        )

    mock_service.mark_run_failed.assert_awaited_once()
    assert len(mock_compute_slots["release"]) == 1


# ---------------------------------------------------------------------------
# Lease release is always attempted
# ---------------------------------------------------------------------------


async def test_release_failure_does_not_mask_original_error(
    mock_service: MagicMock,
    mock_redis_pool: MagicMock,
    mock_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    mock_run_id: str,
    mock_portfolio_id: str,
) -> None:
    async def _acquire(*a, **kw):
        return "test-lease-id"

    async def _release_boom(*a, **kw):
        raise RuntimeError("release exploded")

    async def _renew(*a, **kw):
        return None

    monkeypatch.setattr(portfolio_job, "acquire_compute_slots", _acquire)
    monkeypatch.setattr(portfolio_job, "release_compute_slots", _release_boom)
    monkeypatch.setattr(portfolio_job, "renew_compute_slots", _renew)

    mock_service.run_portfolio_backtest.side_effect = RuntimeError("original failure")

    # Release error is swallowed; original RuntimeError propagates unchanged.
    with pytest.raises(RuntimeError, match="original failure"):
        await portfolio_job.run_portfolio_job({}, mock_run_id, mock_portfolio_id)
