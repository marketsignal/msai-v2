"""Tests for _mark_backtest_failed classifier wiring."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from msai.models.backtest import Backtest
from msai.workers.backtest_job import _mark_backtest_failed


@pytest.fixture()
def fake_row() -> Backtest:
    return Backtest(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_code_hash="x" * 64,
        config={},
        instruments=["ES.n.0"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 15),
        status="running",
    )


class TestMarkBacktestFailed:
    """[iter-2 P2] Wiring tests cover each classifier branch so the
    persisted envelope contract is verified end-to-end at the worker
    boundary — not just in the classifier's unit tests.
    """

    async def _invoke_mark_failed(self, row: Backtest, exc: BaseException) -> None:
        """Helper: patches session factory to return our fake row + runs."""
        session = AsyncMock()
        session.get.return_value = row
        with patch("msai.workers.backtest_job.async_session_factory") as factory:
            factory.return_value.__aenter__.return_value = session
            await _mark_backtest_failed(
                backtest_id=str(row.id),
                exc=exc,
                instruments=list(row.instruments),
                start_date=row.start_date,
                end_date=row.end_date,
            )

    async def test_missing_data_writes_full_envelope(self, fake_row: Backtest) -> None:
        exc = FileNotFoundError(
            "No raw Parquet files found for 'ES' under /app/data/parquet/stocks/ES."
        )
        await self._invoke_mark_failed(fake_row, exc)

        assert fake_row.status == "failed"
        assert fake_row.error_code == "missing_data"
        assert fake_row.error_message  # raw stays populated
        assert fake_row.error_public_message
        assert "/app/" not in fake_row.error_public_message  # sanitized
        assert fake_row.error_suggested_action
        assert "msai ingest" in fake_row.error_suggested_action
        assert fake_row.error_remediation is not None
        assert fake_row.error_remediation["kind"] == "ingest_data"
        assert fake_row.error_remediation["symbols"] == ["ES.n.0"]
        assert fake_row.completed_at is not None

    async def test_wrapped_import_error_persists_as_strategy_import(
        self, fake_row: Backtest
    ) -> None:
        tb = (
            "Traceback (most recent call last):\n"
            '  File "strategies/broken.py", line 1, in <module>\n'
            "ModuleNotFoundError: No module named 'missing_dep'"
        )
        await self._invoke_mark_failed(fake_row, RuntimeError(tb))

        assert fake_row.status == "failed"
        assert fake_row.error_code == "strategy_import_error"
        assert fake_row.error_public_message
        assert fake_row.error_suggested_action is None
        assert fake_row.error_remediation is None

    async def test_wrapped_engine_crash_persists_as_engine_crash(self, fake_row: Backtest) -> None:
        tb = (
            "Traceback (most recent call last):\n"
            '  File "/app/.venv/.../nautilus.py", line 500, in _run\n'
            '    raise ValueError("bar_type mismatch")\n'
            "ValueError: bar_type mismatch"
        )
        await self._invoke_mark_failed(fake_row, RuntimeError(tb))

        assert fake_row.status == "failed"
        assert fake_row.error_code == "engine_crash"
        assert fake_row.error_public_message
        assert "/app/" not in fake_row.error_public_message  # sanitized
        assert fake_row.error_remediation is None

    async def test_timeout_persists_as_timeout(self, fake_row: Backtest) -> None:
        await self._invoke_mark_failed(fake_row, TimeoutError("Backtest exceeded 900s wall clock"))
        assert fake_row.error_code == "timeout"
        assert fake_row.error_public_message
        assert fake_row.error_remediation is None

    async def test_unknown_fallback_persists_as_unknown(self, fake_row: Backtest) -> None:
        await self._invoke_mark_failed(fake_row, KeyboardInterrupt())
        assert fake_row.error_code == "unknown"
        assert fake_row.error_public_message  # never blank (US-006)
        assert fake_row.error_suggested_action is None
        assert fake_row.error_remediation is None
