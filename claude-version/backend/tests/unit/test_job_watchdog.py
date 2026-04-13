"""Tests for the job watchdog service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from msai.services.job_watchdog import (
    _check_job_health,
    _scan_backtests,
    _scan_research_jobs,
    run_watchdog_once,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_backtest(
    *,
    status: str = "running",
    heartbeat_at: datetime | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Create a mock Backtest row."""
    bt = MagicMock()
    bt.id = uuid4()
    bt.status = status
    bt.heartbeat_at = heartbeat_at
    bt.created_at = created_at or datetime.now(UTC)
    bt.error_message = None
    bt.completed_at = None
    return bt


def _make_research_job(
    *,
    status: str = "running",
    heartbeat_at: datetime | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Create a mock ResearchJob row."""
    rj = MagicMock()
    rj.id = uuid4()
    rj.status = status
    rj.heartbeat_at = heartbeat_at
    rj.created_at = created_at or datetime.now(UTC)
    rj.error_message = None
    rj.completed_at = None
    return rj


# ---------------------------------------------------------------------------
# _check_job_health — pure function tests
# ---------------------------------------------------------------------------


class TestCheckJobHealth:
    """Tests for the _check_job_health helper."""

    def test_stale_running_returns_reason(self) -> None:
        now = datetime.now(UTC)
        result = _check_job_health(
            status="running",
            heartbeat_at=now - timedelta(seconds=1200),
            created_at=now - timedelta(seconds=1800),
            stale_cutoff=now - timedelta(seconds=600),
            pending_cutoff=now - timedelta(seconds=600),
            now=now,
        )
        assert result is not None
        assert "no heartbeat" in result
        assert "1200" in result

    def test_fresh_running_returns_none(self) -> None:
        now = datetime.now(UTC)
        result = _check_job_health(
            status="running",
            heartbeat_at=now - timedelta(seconds=60),
            created_at=now - timedelta(seconds=300),
            stale_cutoff=now - timedelta(seconds=600),
            pending_cutoff=now - timedelta(seconds=600),
            now=now,
        )
        assert result is None

    def test_running_with_no_heartbeat_returns_none(self) -> None:
        """Running job with heartbeat_at=None should NOT be flagged (may have just started)."""
        now = datetime.now(UTC)
        result = _check_job_health(
            status="running",
            heartbeat_at=None,
            created_at=now - timedelta(seconds=1200),
            stale_cutoff=now - timedelta(seconds=600),
            pending_cutoff=now - timedelta(seconds=600),
            now=now,
        )
        assert result is None

    def test_stuck_pending_returns_reason(self) -> None:
        now = datetime.now(UTC)
        result = _check_job_health(
            status="pending",
            heartbeat_at=None,
            created_at=now - timedelta(seconds=1200),
            stale_cutoff=now - timedelta(seconds=600),
            pending_cutoff=now - timedelta(seconds=600),
            now=now,
        )
        assert result is not None
        assert "stuck in pending" in result
        assert "1200" in result

    def test_fresh_pending_returns_none(self) -> None:
        now = datetime.now(UTC)
        result = _check_job_health(
            status="pending",
            heartbeat_at=None,
            created_at=now - timedelta(seconds=60),
            stale_cutoff=now - timedelta(seconds=600),
            pending_cutoff=now - timedelta(seconds=600),
            now=now,
        )
        assert result is None


# ---------------------------------------------------------------------------
# _scan_backtests
# ---------------------------------------------------------------------------


class TestScanBacktests:
    """Tests for _scan_backtests."""

    @pytest.mark.asyncio
    async def test_stale_running_backtest_marked_failed(self) -> None:
        # Arrange
        now = datetime.now(UTC)
        bt = _make_backtest(
            status="running",
            heartbeat_at=now - timedelta(seconds=1200),
            created_at=now - timedelta(seconds=1800),
        )

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [bt]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        # Act
        cleaned = await _scan_backtests(session)

        # Assert
        assert cleaned == 1
        assert bt.status == "failed"
        assert bt.error_message is not None
        assert "no heartbeat" in bt.error_message
        assert bt.completed_at is not None

    @pytest.mark.asyncio
    async def test_fresh_running_backtest_left_alone(self) -> None:
        # Arrange
        now = datetime.now(UTC)
        bt = _make_backtest(
            status="running",
            heartbeat_at=now - timedelta(seconds=60),
            created_at=now - timedelta(seconds=300),
        )

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [bt]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        # Act
        cleaned = await _scan_backtests(session)

        # Assert
        assert cleaned == 0
        assert bt.status == "running"
        assert bt.error_message is None

    @pytest.mark.asyncio
    async def test_stuck_pending_backtest_marked_failed(self) -> None:
        # Arrange
        now = datetime.now(UTC)
        bt = _make_backtest(
            status="pending",
            heartbeat_at=None,
            created_at=now - timedelta(seconds=1200),
        )

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [bt]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        # Act
        cleaned = await _scan_backtests(session)

        # Assert
        assert cleaned == 1
        assert bt.status == "failed"
        assert "stuck in pending" in bt.error_message

    @pytest.mark.asyncio
    async def test_fresh_pending_backtest_left_alone(self) -> None:
        # Arrange
        now = datetime.now(UTC)
        bt = _make_backtest(
            status="pending",
            heartbeat_at=None,
            created_at=now - timedelta(seconds=60),
        )

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [bt]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        # Act
        cleaned = await _scan_backtests(session)

        # Assert
        assert cleaned == 0
        assert bt.status == "pending"


# ---------------------------------------------------------------------------
# _scan_research_jobs
# ---------------------------------------------------------------------------


class TestScanResearchJobs:
    """Tests for _scan_research_jobs."""

    @pytest.mark.asyncio
    async def test_stale_running_research_job_marked_failed(self) -> None:
        # Arrange
        now = datetime.now(UTC)
        rj = _make_research_job(
            status="running",
            heartbeat_at=now - timedelta(seconds=1200),
            created_at=now - timedelta(seconds=1800),
        )

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [rj]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        # Act
        cleaned = await _scan_research_jobs(session)

        # Assert
        assert cleaned == 1
        assert rj.status == "failed"
        assert "no heartbeat" in rj.error_message
        assert rj.completed_at is not None

    @pytest.mark.asyncio
    async def test_fresh_running_research_job_left_alone(self) -> None:
        # Arrange
        now = datetime.now(UTC)
        rj = _make_research_job(
            status="running",
            heartbeat_at=now - timedelta(seconds=60),
        )

        session = AsyncMock()
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [rj]
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=execute_result)

        # Act
        cleaned = await _scan_research_jobs(session)

        # Assert
        assert cleaned == 0
        assert rj.status == "running"


# ---------------------------------------------------------------------------
# run_watchdog_once — integration of both scanners
# ---------------------------------------------------------------------------


class TestRunWatchdogOnce:
    """Tests for the top-level run_watchdog_once function."""

    @pytest.mark.asyncio
    async def test_returns_correct_counts(self) -> None:
        """run_watchdog_once returns a dict with counts from both scanners."""
        now = datetime.now(UTC)
        stale_bt = _make_backtest(
            status="running",
            heartbeat_at=now - timedelta(seconds=1200),
            created_at=now - timedelta(seconds=1800),
        )
        fresh_bt = _make_backtest(
            status="running",
            heartbeat_at=now - timedelta(seconds=30),
            created_at=now - timedelta(seconds=300),
        )
        stale_rj = _make_research_job(
            status="running",
            heartbeat_at=now - timedelta(seconds=900),
            created_at=now - timedelta(seconds=1200),
        )

        call_count = 0

        async def mock_execute(stmt: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            scalars = MagicMock()
            # First call is backtests, second is research_jobs
            if call_count == 1:
                scalars.all.return_value = [stale_bt, fresh_bt]
            else:
                scalars.all.return_value = [stale_rj]
            result = MagicMock()
            result.scalars.return_value = scalars
            return result

        mock_session = AsyncMock()
        mock_session.execute = mock_execute
        mock_session.commit = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "msai.services.job_watchdog.async_session_factory",
            return_value=mock_ctx,
        ):
            result = await run_watchdog_once()

        assert result == {"backtests_cleaned": 1, "research_cleaned": 1}
        mock_session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_stale_jobs_returns_zeros(self) -> None:
        """When all jobs are healthy, both counts are 0."""
        now = datetime.now(UTC)
        fresh_bt = _make_backtest(
            status="running",
            heartbeat_at=now - timedelta(seconds=30),
        )

        call_count = 0

        async def mock_execute(stmt: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            scalars = MagicMock()
            if call_count == 1:
                scalars.all.return_value = [fresh_bt]
            else:
                scalars.all.return_value = []
            result = MagicMock()
            result.scalars.return_value = scalars
            return result

        mock_session = AsyncMock()
        mock_session.execute = mock_execute
        mock_session.commit = AsyncMock()

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "msai.services.job_watchdog.async_session_factory",
            return_value=mock_ctx,
        ):
            result = await run_watchdog_once()

        assert result == {"backtests_cleaned": 0, "research_cleaned": 0}
