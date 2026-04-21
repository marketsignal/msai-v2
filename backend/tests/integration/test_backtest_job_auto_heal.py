"""Integration tests for the backtest worker's auto-heal retry path.

Task B8 wires :func:`msai.services.backtests.auto_heal.run_auto_heal`
into :func:`msai.workers.backtest_job.run_backtest_job` via a bounded
retry-once loop. These tests cover the five terminal shapes the loop
must produce:

1. SUCCESS after heal → backtest completes and ``ensure_catalog_data``
   is called twice (first raises FNF, second succeeds).
2. GUARDRAIL_REJECTED → backtest marked failed, ``error_code =
   missing_data``, envelope carries the guardrail message.
3. TIMEOUT → backtest marked failed, ``error_code = timeout`` (via
   ``TimeoutError`` classifier branch).
4. INGEST_FAILED → backtest marked failed, ``error_code = engine_crash``
   (via ``RuntimeError`` classifier branch).
5. Non-FNF failure from the BacktestRunner → no auto-heal attempted,
   envelope built from the raw exception.

A sixth test asserts the iter-1 P1-c fix: ``_start_backtest`` runs
exactly once, so ``attempt`` is 1 after the success-after-heal path
(not 2).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pandas as pd
import pytest

from msai.models.backtest import Backtest
from msai.services.backtests.auto_heal import AutoHealOutcome, AutoHealResult
from msai.services.nautilus.backtest_runner import BacktestResult
from msai.workers.backtest_job import run_backtest_job

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backtest_row() -> Backtest:
    """Return a minimal in-memory ``Backtest`` row for the worker to mutate."""
    return Backtest(
        id=uuid4(),
        strategy_id=uuid4(),
        strategy_code_hash="a" * 64,
        config={"asset_class": "stocks"},
        instruments=["AAPL"],
        start_date=date(2024, 1, 2),
        end_date=date(2024, 3, 15),
        status="pending",
        progress=0,
        attempt=0,
    )


def _make_backtest_result() -> BacktestResult:
    """Return a benign :class:`BacktestResult` the worker can serialize."""
    return BacktestResult(
        metrics={"num_trades": 0, "total_return": 0.0},
        account_df=pd.DataFrame(),
        orders_df=pd.DataFrame(),
        positions_df=pd.DataFrame(),
    )


class _SessionFactoryStub:
    """Async-context-manager stub that yields a single mock session.

    The worker opens several short-lived sessions (``_start_backtest``,
    ``_persist_lineage``, heartbeat, ``_finalize_backtest``,
    ``_mark_backtest_failed``). All of them go through
    ``async_session_factory()`` so one patch-point is enough; we hand
    back the same mock on every ``__aenter__`` call.
    """

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> _SessionFactoryStub:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture()
def fake_row() -> Backtest:
    return _make_backtest_row()


@pytest.fixture()
def session_factory(fake_row: Backtest) -> _SessionFactoryStub:
    """Patch-ready async session factory returning a single shared row."""
    session = AsyncMock()
    session.get.return_value = fake_row
    session.commit = AsyncMock()
    return _SessionFactoryStub(session)


@pytest.fixture()
def arq_ctx() -> dict[str, Any]:
    """Mimic the arq worker context — only ``ctx["redis"]`` is read."""
    return {"redis": MagicMock(name="arq_redis_pool")}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAutoHealRetryLoop:
    """Task B8 — retry-once wiring for the missing-data path."""

    async def test_backtest_job_invokes_auto_heal_on_missing_data(
        self,
        fake_row: Backtest,
        session_factory: _SessionFactoryStub,
        arq_ctx: dict[str, Any],
    ) -> None:
        """FNF on first attempt + SUCCESS outcome → backtest completes.

        ``ensure_catalog_data`` must be called twice (once before the
        heal, once after). ``run_auto_heal`` is called exactly once.
        """
        ensure_mock = MagicMock(
            side_effect=[
                FileNotFoundError(
                    "No raw Parquet files found for 'AAPL' "
                    "under /app/data/parquet/stocks/AAPL. Run..."
                ),
                ["AAPL.NASDAQ"],
            ]
        )
        describe_mock = MagicMock(return_value={"files": []})
        runner_instance = MagicMock()
        runner_instance.run = MagicMock(return_value=_make_backtest_result())
        auto_heal_mock = AsyncMock(
            return_value=AutoHealResult(
                outcome=AutoHealOutcome.SUCCESS,
                asset_class="stocks",
                resolved_instrument_ids=["AAPL.NASDAQ"],
                reason_human=None,
            )
        )

        with (
            patch("msai.workers.backtest_job.async_session_factory", session_factory),
            patch("msai.workers.backtest_job.ensure_catalog_data", ensure_mock),
            patch("msai.workers.backtest_job.describe_catalog", describe_mock),
            patch("msai.workers.backtest_job.BacktestRunner", return_value=runner_instance),
            patch("msai.workers.backtest_job.run_auto_heal", auto_heal_mock),
            patch("msai.workers.backtest_job.ReportGenerator") as report_gen_cls,
        ):
            report_gen_cls.return_value.generate_tearsheet.return_value = "<html/>"
            report_gen_cls.return_value.save_report.return_value = "/tmp/report.html"  # noqa: S108
            await run_backtest_job(
                arq_ctx,
                str(fake_row.id),
                "/tmp/strategy.py",  # noqa: S108
                {"asset_class": "stocks"},
            )

        assert ensure_mock.call_count == 2
        auto_heal_mock.assert_awaited_once()
        assert fake_row.status == "completed"

    async def test_attempt_counter_increments_once_not_twice_on_success_after_heal(
        self,
        fake_row: Backtest,
        session_factory: _SessionFactoryStub,
        arq_ctx: dict[str, Any],
    ) -> None:
        """iter-1 P1-c: ``_start_backtest`` runs once, so ``attempt`` is 1."""
        ensure_mock = MagicMock(
            side_effect=[
                FileNotFoundError("No raw Parquet files found for 'AAPL' ..."),
                ["AAPL.NASDAQ"],
            ]
        )
        runner_instance = MagicMock()
        runner_instance.run = MagicMock(return_value=_make_backtest_result())
        auto_heal_mock = AsyncMock(
            return_value=AutoHealResult(
                outcome=AutoHealOutcome.SUCCESS,
                asset_class="stocks",
                resolved_instrument_ids=["AAPL.NASDAQ"],
                reason_human=None,
            )
        )

        with (
            patch("msai.workers.backtest_job.async_session_factory", session_factory),
            patch("msai.workers.backtest_job.ensure_catalog_data", ensure_mock),
            patch("msai.workers.backtest_job.describe_catalog", return_value={"files": []}),
            patch("msai.workers.backtest_job.BacktestRunner", return_value=runner_instance),
            patch("msai.workers.backtest_job.run_auto_heal", auto_heal_mock),
            patch("msai.workers.backtest_job.ReportGenerator") as report_gen_cls,
        ):
            report_gen_cls.return_value.generate_tearsheet.return_value = "<html/>"
            report_gen_cls.return_value.save_report.return_value = "/tmp/report.html"  # noqa: S108
            await run_backtest_job(
                arq_ctx,
                str(fake_row.id),
                "/tmp/strategy.py",  # noqa: S108
                {"asset_class": "stocks"},
            )

        assert fake_row.attempt == 1, (
            f"attempt counter incremented twice — "
            f"_start_backtest should only run on the first attempt "
            f"(got {fake_row.attempt})"
        )

    async def test_backtest_job_guardrail_rejection_marks_failed_with_envelope(
        self,
        fake_row: Backtest,
        session_factory: _SessionFactoryStub,
        arq_ctx: dict[str, Any],
    ) -> None:
        """GUARDRAIL_REJECTED → failed row, ``error_code = missing_data``."""
        ensure_mock = MagicMock(
            side_effect=FileNotFoundError(
                "No raw Parquet files found for 'AAPL' under /app/data/parquet/stocks/AAPL. Run..."
            )
        )
        auto_heal_mock = AsyncMock(
            return_value=AutoHealResult(
                outcome=AutoHealOutcome.GUARDRAIL_REJECTED,
                asset_class="stocks",
                resolved_instrument_ids=None,
                reason_human="Backtest range exceeds 10-year cap (12.0 years).",
            )
        )

        with (
            patch("msai.workers.backtest_job.async_session_factory", session_factory),
            patch("msai.workers.backtest_job.ensure_catalog_data", ensure_mock),
            patch("msai.workers.backtest_job.run_auto_heal", auto_heal_mock),
        ):
            await run_backtest_job(
                arq_ctx,
                str(fake_row.id),
                "/tmp/strategy.py",  # noqa: S108
                {"asset_class": "stocks"},
            )

        assert fake_row.status == "failed"
        assert fake_row.error_code == "missing_data"
        assert fake_row.error_public_message
        assert "10-year cap" in (fake_row.error_message or "")

    async def test_backtest_job_timeout_outcome_classifies_as_timeout(
        self,
        fake_row: Backtest,
        session_factory: _SessionFactoryStub,
        arq_ctx: dict[str, Any],
    ) -> None:
        """TIMEOUT outcome → ``TimeoutError`` → ``error_code = timeout``."""
        ensure_mock = MagicMock(
            side_effect=FileNotFoundError(
                "No raw Parquet files found for 'AAPL' under /app/data/parquet/stocks/AAPL. Run..."
            )
        )
        auto_heal_mock = AsyncMock(
            return_value=AutoHealResult(
                outcome=AutoHealOutcome.TIMEOUT,
                asset_class="stocks",
                resolved_instrument_ids=None,
                reason_human="Data download exceeded 30-minute cap.",
            )
        )

        with (
            patch("msai.workers.backtest_job.async_session_factory", session_factory),
            patch("msai.workers.backtest_job.ensure_catalog_data", ensure_mock),
            patch("msai.workers.backtest_job.run_auto_heal", auto_heal_mock),
        ):
            await run_backtest_job(
                arq_ctx,
                str(fake_row.id),
                "/tmp/strategy.py",  # noqa: S108
                {"asset_class": "stocks"},
            )

        assert fake_row.status == "failed"
        assert fake_row.error_code == "timeout"

    async def test_backtest_job_ingest_failed_outcome_classifies_as_engine_crash(
        self,
        fake_row: Backtest,
        session_factory: _SessionFactoryStub,
        arq_ctx: dict[str, Any],
    ) -> None:
        """INGEST_FAILED → ``RuntimeError`` → ``error_code = engine_crash``."""
        ensure_mock = MagicMock(
            side_effect=FileNotFoundError(
                "No raw Parquet files found for 'AAPL' under /app/data/parquet/stocks/AAPL. Run..."
            )
        )
        auto_heal_mock = AsyncMock(
            return_value=AutoHealResult(
                outcome=AutoHealOutcome.INGEST_FAILED,
                asset_class="stocks",
                resolved_instrument_ids=None,
                reason_human="Ingest provider returned an error; see worker logs.",
            )
        )

        with (
            patch("msai.workers.backtest_job.async_session_factory", session_factory),
            patch("msai.workers.backtest_job.ensure_catalog_data", ensure_mock),
            patch("msai.workers.backtest_job.run_auto_heal", auto_heal_mock),
        ):
            await run_backtest_job(
                arq_ctx,
                str(fake_row.id),
                "/tmp/strategy.py",  # noqa: S108
                {"asset_class": "stocks"},
            )

        assert fake_row.status == "failed"
        assert fake_row.error_code == "engine_crash"

    async def test_backtest_job_non_missing_data_failure_bypasses_auto_heal(
        self,
        fake_row: Backtest,
        session_factory: _SessionFactoryStub,
        arq_ctx: dict[str, Any],
    ) -> None:
        """Generic Exception in the runner → no heal, classifier's normal path."""
        ensure_mock = MagicMock(return_value=["AAPL.NASDAQ"])
        runner_instance = MagicMock()
        runner_instance.run = MagicMock(
            side_effect=RuntimeError("Nautilus subprocess died in node.run()")
        )
        auto_heal_mock = AsyncMock()

        with (
            patch("msai.workers.backtest_job.async_session_factory", session_factory),
            patch("msai.workers.backtest_job.ensure_catalog_data", ensure_mock),
            patch("msai.workers.backtest_job.describe_catalog", return_value={"files": []}),
            patch("msai.workers.backtest_job.BacktestRunner", return_value=runner_instance),
            patch("msai.workers.backtest_job.run_auto_heal", auto_heal_mock),
        ):
            await run_backtest_job(
                arq_ctx,
                str(fake_row.id),
                "/tmp/strategy.py",  # noqa: S108
                {"asset_class": "stocks"},
            )

        auto_heal_mock.assert_not_called()
        assert fake_row.status == "failed"
        assert fake_row.error_code == "engine_crash"

    async def test_backtest_job_run_auto_heal_raises_still_marks_failed(
        self,
        fake_row: Backtest,
        session_factory: _SessionFactoryStub,
        arq_ctx: dict[str, Any],
    ) -> None:
        """run_auto_heal raising (e.g., Redis connection error) must NOT leave
        the backtest stuck in ``running``.

        Regression for Codex PR review P1 2026-04-21 — if the orchestrator
        itself raises inside the ``except FileNotFoundError`` handler, Python
        doesn't re-catch it. Without the inner try/except wrap, the exception
        escapes the while-loop past the ``if terminal_exc is not None:`` block
        and ``_handle_terminal_failure`` is never called.
        """
        import redis.exceptions

        ensure_mock = MagicMock(
            side_effect=FileNotFoundError(
                "No raw Parquet files found for 'AAPL' under /app/data/parquet/stocks/AAPL. Run..."
            )
        )
        auto_heal_mock = AsyncMock(
            side_effect=redis.exceptions.ConnectionError("Redis unavailable")
        )

        with (
            patch("msai.workers.backtest_job.async_session_factory", session_factory),
            patch("msai.workers.backtest_job.ensure_catalog_data", ensure_mock),
            patch("msai.workers.backtest_job.run_auto_heal", auto_heal_mock),
        ):
            await run_backtest_job(
                arq_ctx,
                str(fake_row.id),
                "/tmp/strategy.py",  # noqa: S108
                {"asset_class": "stocks"},
            )

        # Backtest MUST be marked failed — not left stuck as running.
        assert fake_row.status == "failed"
        # redis ConnectionError is a RuntimeError subclass → classifier maps to
        # engine_crash. The exact code isn't the point; "row isn't running" is.
        assert fake_row.error_message is not None
        assert "Redis unavailable" in fake_row.error_message
