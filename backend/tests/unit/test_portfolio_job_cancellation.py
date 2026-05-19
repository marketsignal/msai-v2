"""F2 unit tests for the cancellation + progress helpers in ``portfolio_job``.

The helpers themselves are tiny (a ``session.get`` + status compare; or a
``run.metrics`` patch + heartbeat refresh) — these tests just lock the
contract so the optimizer-side wiring (in
:mod:`msai.services.portfolio.orchestration`) and the worker-side sync
bridge (``_sync_cancel_check`` / ``_sync_progress`` inside
``run_portfolio_job``) can rely on them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from msai.models.portfolio_enums import PortfolioRunStatus
from msai.workers.portfolio_job import (
    _check_cancel_flag,
    _portfolio_progress_callback,
)


@pytest.mark.asyncio
async def test_check_cancel_flag_returns_false_when_status_is_running() -> None:
    """A run still ``running`` is not cancelled."""
    run_id = uuid4()
    run = MagicMock()
    run.status = PortfolioRunStatus.RUNNING.value
    session = MagicMock()
    session.get = AsyncMock(return_value=run)

    assert await _check_cancel_flag(session, run_id) is False


@pytest.mark.asyncio
async def test_check_cancel_flag_returns_true_when_status_is_canceled() -> None:
    """Operator-cancelled run is detected via the canonical ``canceled`` status."""
    run_id = uuid4()
    run = MagicMock()
    run.status = PortfolioRunStatus.CANCELED.value
    session = MagicMock()
    session.get = AsyncMock(return_value=run)

    assert await _check_cancel_flag(session, run_id) is True


@pytest.mark.asyncio
async def test_check_cancel_flag_returns_false_when_row_missing() -> None:
    """Missing row is not "cancelled" — the worker has other guards for that case.

    The optimizer's cancel loop calls this every trial; returning ``True`` on
    a missing row would silently break (the row may have been deleted but
    the trial loop should not stop; the worker's enclosing exception
    handlers surface the missing-row case).
    """
    run_id = uuid4()
    session = MagicMock()
    session.get = AsyncMock(return_value=None)

    assert await _check_cancel_flag(session, run_id) is False


@pytest.mark.asyncio
async def test_progress_callback_updates_metrics_and_heartbeat() -> None:
    """Progress writes ``metrics['progress']`` + refreshes ``heartbeat_at``."""
    run_id = uuid4()
    run = MagicMock()
    run.metrics = {"existing": "value"}
    run.heartbeat_at = datetime(2026, 1, 1, tzinfo=UTC)
    session = MagicMock()
    session.get = AsyncMock(return_value=run)
    session.commit = AsyncMock()

    await _portfolio_progress_callback(session, run_id, 42, "window 3/10")

    assert run.metrics["progress"] == 42
    assert run.metrics["progress_message"] == "window 3/10"
    # Existing keys preserved (the callback merges, not replaces).
    assert run.metrics["existing"] == "value"
    # heartbeat_at moved forward (any non-2026-01-01 timestamp is fine).
    assert run.heartbeat_at > datetime(2026, 1, 1, tzinfo=UTC)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_callback_handles_missing_metrics_dict() -> None:
    """``metrics`` may be None on a fresh row — the callback must coerce."""
    run_id = uuid4()
    run = MagicMock()
    run.metrics = None
    session = MagicMock()
    session.get = AsyncMock(return_value=run)
    session.commit = AsyncMock()

    await _portfolio_progress_callback(session, run_id, 10, "starting")

    assert run.metrics == {"progress": 10, "progress_message": "starting"}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_progress_callback_silently_noop_on_missing_row() -> None:
    """Missing row is a benign race — write skipped, no exception, no commit."""
    run_id = uuid4()
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    await _portfolio_progress_callback(session, run_id, 50, "halfway")

    session.commit.assert_not_called()
