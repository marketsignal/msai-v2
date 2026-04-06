from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.models import Backtest, Trade
from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.report_generator import ReportGenerator

logger = get_logger("workers.backtest")


async def run_backtest(ctx: dict, backtest_id: str, strategy_path: str, config: dict) -> None:
    _ = ctx

    async with async_session_factory() as session:
        backtest = await session.get(Backtest, backtest_id)
        if backtest is None:
            logger.error("backtest_not_found", backtest_id=backtest_id)
            return
        backtest.status = "running"
        backtest.progress = 10
        backtest.started_at = datetime.now(UTC)
        await session.commit()

    try:
        runner = BacktestRunner()
        result = runner.run(
            strategy_path=strategy_path,
            config=config,
            instruments=backtest.instruments,
            start_date=backtest.start_date.isoformat(),
            end_date=backtest.end_date.isoformat(),
            data_path=settings.parquet_root,
            timeout_seconds=settings.backtest_timeout_seconds,
        )

        account_returns = (
            result.account_df["returns"]
            if "returns" in result.account_df.columns
            else pd.Series(dtype=float)
        )
        report_generator = ReportGenerator(settings.reports_root)
        html = report_generator.generate_tearsheet(account_returns)
        report_path = report_generator.save_report(html, backtest_id)

        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            if row.strategy_id is None:
                raise RuntimeError("Backtest missing strategy_id")

            row.status = "completed"
            row.progress = 100
            row.metrics = result.metrics
            row.report_path = str(report_path)
            row.completed_at = datetime.now(UTC)

            for trade in result.orders_df.to_dict(orient="records"):
                timestamp = _trade_timestamp_utc(trade)
                session.add(
                    Trade(
                        backtest_id=row.id,
                        deployment_id=None,
                        strategy_id=row.strategy_id,
                        strategy_code_hash=row.strategy_code_hash,
                        instrument=str(trade.get("symbol", "UNKNOWN")),
                        side=str(trade.get("side", "BUY")),
                        quantity=float(trade.get("quantity", 0.0)),
                        price=float(trade.get("price", 0.0)),
                        commission=0.0,
                        pnl=float(trade.get("pnl", 0.0)),
                        is_live=False,
                        executed_at=timestamp,
                    )
                )

            await session.commit()

    except Exception as exc:
        logger.exception("backtest_failed", backtest_id=backtest_id, error=str(exc))
        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            row.status = "failed"
            row.error_message = str(exc)
            row.completed_at = datetime.now(UTC)
            await session.commit()


def _trade_timestamp_utc(trade: dict[str, object]) -> datetime:
    for candidate in ("timestamp", "ts_event", "ts_last", "ts_init"):
        raw = trade.get(candidate)
        if raw is None:
            continue
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")
        if not pd.isna(parsed):
            return parsed.to_pydatetime()
    return datetime.now(UTC)
