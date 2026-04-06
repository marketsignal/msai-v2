"""arq worker function for executing backtests.

Orchestrates the full backtest lifecycle:
1. Update status to ``running`` in DB
2. Load strategy class from filesystem
3. Load bar data from Parquet
4. Run the BacktestRunner
5. Generate QuantStats report
6. Save results and trades to DB
7. Update status to ``completed`` (or ``failed`` on error)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models.backtest import Backtest
from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.nautilus.catalog import NautilusCatalog
from msai.services.report_generator import ReportGenerator
from msai.services.strategy_registry import load_strategy_class

log = get_logger(__name__)


async def run_backtest_job(
    ctx: dict[str, Any],
    backtest_id: str,
    strategy_path: str,
    config: dict[str, Any],
) -> None:
    """Execute a full backtest job.

    This is the primary arq worker function registered in
    :class:`WorkerSettings`. It handles the complete lifecycle from
    loading data through generating the report.

    Args:
        ctx: arq worker context (contains Redis pool, etc.).
        backtest_id: UUID string of the Backtest record.
        strategy_path: Filesystem path to the strategy module,
            optionally followed by ``::ClassName``.
        config: Strategy configuration parameters. Must include:
            - ``instrument`` (str): Symbol to backtest.
            - ``start_date`` (str): ISO-8601 start date.
            - ``end_date`` (str): ISO-8601 end date.
            - ``asset_class`` (str, optional): Defaults to ``"stocks"``.
            - ``initial_cash`` (float, optional): Defaults to ``100000.0``.
            - ``strategy_config`` (dict, optional): Kwargs for the strategy constructor.
    """
    log.info("backtest_job_started", backtest_id=backtest_id, strategy_path=strategy_path)

    try:
        # Mark backtest as running in the database
        async with async_session_factory() as session:
            backtest = await session.get(Backtest, backtest_id)
            if backtest:
                backtest.status = "running"
                backtest.started_at = datetime.now(timezone.utc)
                await session.commit()

        # Parse strategy path: "path/to/file.py::ClassName" or just "path/to/file.py"
        class_name = "EMACrossStrategy"  # default
        module_path_str = strategy_path
        if "::" in strategy_path:
            module_path_str, class_name = strategy_path.rsplit("::", 1)

        module_path = Path(module_path_str)

        # 1. Load strategy class
        strategy_cls = load_strategy_class(module_path, class_name)

        # 2. Load bar data from Parquet catalog
        catalog = NautilusCatalog(settings.data_root)
        instrument = config.get("instrument", "AAPL")
        asset_class = config.get("asset_class", "stocks")
        start_date = config.get("start_date")
        end_date = config.get("end_date")

        bars_df = catalog.load_bars(
            symbol=instrument,
            start=start_date,
            end=end_date,
            asset_class=asset_class,
        )

        if bars_df.empty:
            log.warning("backtest_no_data", backtest_id=backtest_id, instrument=instrument)
            await _mark_backtest_failed(
                backtest_id, ValueError(f"No bar data found for {instrument}")
            )
            return

        # 3. Run the backtest
        runner = BacktestRunner()
        strategy_config = config.get("strategy_config", {})
        initial_cash = config.get("initial_cash", 100_000.0)

        result = runner.run(
            strategy_class=strategy_cls,
            config=strategy_config,
            bars_df=bars_df,
            initial_cash=initial_cash,
        )

        # 4. Generate QuantStats report
        report_gen = ReportGenerator()
        html = report_gen.generate_tearsheet(
            returns=result.returns_series,
            title=f"Backtest {backtest_id}",
        )
        report_path = report_gen.save_report(html, backtest_id, settings.data_root)

        # 5. Mark backtest as completed in the database
        async with async_session_factory() as session:
            backtest = await session.get(Backtest, backtest_id)
            if backtest:
                backtest.status = "completed"
                backtest.metrics = result.metrics
                backtest.report_path = str(report_path)
                backtest.completed_at = datetime.now(timezone.utc)
                await session.commit()

        log.info(
            "backtest_job_completed",
            backtest_id=backtest_id,
            num_trades=result.metrics.get("num_trades", 0),
            total_return=result.metrics.get("total_return", 0.0),
            report_path=report_path,
        )

    except ImportError as exc:
        log.error("backtest_strategy_load_failed", backtest_id=backtest_id, error=str(exc))
        await _mark_backtest_failed(backtest_id, exc)
        raise
    except FileNotFoundError as exc:
        log.error("backtest_data_not_found", backtest_id=backtest_id, error=str(exc))
        await _mark_backtest_failed(backtest_id, exc)
        raise
    except Exception as exc:
        log.error("backtest_job_failed", backtest_id=backtest_id, error=str(exc))
        await _mark_backtest_failed(backtest_id, exc)
        raise


async def _mark_backtest_failed(backtest_id: str, error: Exception) -> None:
    """Update the Backtest record to ``failed`` status with the error message."""
    try:
        async with async_session_factory() as session:
            backtest = await session.get(Backtest, backtest_id)
            if backtest:
                backtest.status = "failed"
                backtest.error_message = str(error)
                backtest.completed_at = datetime.now(timezone.utc)
                await session.commit()
    except Exception:
        log.exception("backtest_status_update_failed", backtest_id=backtest_id)
