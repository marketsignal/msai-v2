from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, datetime

import pandas as pd

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.core.logging import get_logger
from msai.core.queue import get_redis_pool
from msai.models import Backtest, Trade
from msai.services.backtest_analytics import BacktestAnalyticsService
from msai.services.compute_slots import (
    ComputeSlotUnavailableError,
    acquire_compute_slots,
    release_compute_slots,
    renew_compute_slots,
)
from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.nautilus.strategy_config import prepare_backtest_strategy_config
from msai.services.report_generator import ReportGenerator

logger = get_logger("workers.backtest")


async def run_backtest(ctx: dict, backtest_id: str, strategy_path: str, config: dict) -> None:
    worker_id = str(ctx.get("worker_instance_id") or f"{socket.gethostname()}:{os.getpid()}")
    slot_pool = await get_redis_pool()
    stop_heartbeat = asyncio.Event()
    slot_lease_id: str | None = None

    async with async_session_factory() as session:
        backtest = await session.get(Backtest, backtest_id)
        if backtest is None:
            logger.error("backtest_not_found", backtest_id=backtest_id)
            return
        backtest.status = "running"
        backtest.progress = 10
        backtest.started_at = datetime.now(UTC)
        backtest.worker_id = worker_id
        backtest.attempt = int(backtest.attempt or 0) + 1
        backtest.heartbeat_at = datetime.now(UTC)
        await session.commit()

    try:
        async def _heartbeat_loop() -> None:
            while not stop_heartbeat.is_set():
                await asyncio.sleep(settings.backtest_job_heartbeat_seconds)
                if stop_heartbeat.is_set():
                    return
                if slot_lease_id is not None:
                    await renew_compute_slots(slot_pool, slot_lease_id)
                async with async_session_factory() as session:
                    row = await session.get(Backtest, backtest_id)
                    if row is None or row.status != "running":
                        return
                    row.heartbeat_at = datetime.now(UTC)
                    row.worker_id = worker_id
                    await session.commit()

        heartbeat_task = asyncio.create_task(_heartbeat_loop())

        slot_lease = await acquire_compute_slots(
            slot_pool,
            job_kind="backtest",
            job_id=backtest_id,
            requested_slots=1,
        )
        slot_lease_id = str(slot_lease["lease_id"])

        async with async_session_factory() as session:
            backtest = await session.get(Backtest, backtest_id)
            if backtest is None:
                logger.error("backtest_not_found", backtest_id=backtest_id)
                return
            instrument_definitions = await instrument_service.ensure_backtest_definitions(
                session,
                backtest.instruments,
            )
            await session.commit()

        # Ensure the Nautilus catalog has data for the requested instruments.
        # Converts raw OHLCV Parquet → Nautilus Bar+Instrument catalog on demand.
        instrument_ids = ensure_catalog_data(
            definitions=instrument_definitions,
            raw_parquet_root=settings.parquet_root,
            catalog_root=settings.nautilus_catalog_root,
        )
        strategy_config = prepare_backtest_strategy_config(
            config,
            instrument_ids,
        )

        runner = BacktestRunner()
        result = runner.run(
            strategy_path=strategy_path,
            config=strategy_config,
            instruments=instrument_ids,
            start_date=backtest.start_date.isoformat(),
            end_date=backtest.end_date.isoformat(),
            data_path=settings.nautilus_catalog_root,
            timeout_seconds=settings.backtest_timeout_seconds,
        )

        account_returns = _account_returns_series(result.account_df)
        report_generator = ReportGenerator(settings.reports_root)
        logger.info(
            "backtest_generating_report",
            backtest_id=backtest_id,
            return_points=int(len(account_returns)),
        )
        html = report_generator.generate_tearsheet(account_returns)
        report_path = report_generator.save_report(html, backtest_id)
        analytics_service = BacktestAnalyticsService(settings.backtest_analytics_root)
        logger.info("backtest_saving_analytics", backtest_id=backtest_id, report_path=str(report_path))
        analytics_service.save(
            backtest_id=backtest_id,
            account_df=result.account_df,
            metrics=result.metrics,
            report_path=report_path,
        )

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
            row.heartbeat_at = datetime.now(UTC)

            for trade in result.orders_df.to_dict(orient="records"):
                # Skip unfilled/cancelled orders — no real trade happened.
                filled = trade.get("filled_qty")
                if filled is None:
                    filled = trade.get("quantity", 0.0)
                filled_qty = float(filled)
                if filled_qty == 0.0:
                    continue

                timestamp = _trade_timestamp_utc(trade)
                instrument = trade.get("instrument_id")
                if instrument is None:
                    instrument = trade.get("symbol", "UNKNOWN")
                price = trade.get("avg_px")
                if price is None:
                    price = trade.get("price", 0.0)
                pnl = trade.get("realized_pnl")
                if pnl is None:
                    pnl = trade.get("pnl", 0.0)

                session.add(
                    Trade(
                        backtest_id=row.id,
                        deployment_id=None,
                        strategy_id=row.strategy_id,
                        strategy_code_hash=row.strategy_code_hash,
                        instrument=str(instrument),
                        side=str(trade.get("side", "BUY")),
                        quantity=filled_qty,
                        price=float(price),
                        commission=0.0,
                        pnl=float(pnl),
                        is_live=False,
                        executed_at=timestamp,
                    )
                )

            await session.commit()
            logger.info("backtest_completed", backtest_id=backtest_id)

    except ComputeSlotUnavailableError as exc:
        logger.exception("backtest_slots_unavailable", backtest_id=backtest_id, error=str(exc))
        async with async_session_factory() as session:
            row = await session.get(Backtest, backtest_id)
            if row is None:
                return
            row.status = "failed"
            row.error_message = str(exc)
            row.completed_at = datetime.now(UTC)
            row.heartbeat_at = datetime.now(UTC)
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
            row.heartbeat_at = datetime.now(UTC)
            await session.commit()
    finally:
        stop_heartbeat.set()
        heartbeat_task = locals().get("heartbeat_task")
        if heartbeat_task is not None:
            await heartbeat_task
        if slot_lease_id is not None:
            await release_compute_slots(slot_pool, slot_lease_id)


def _trade_timestamp_utc(trade: dict[str, object]) -> datetime:
    for candidate in ("ts_last", "ts_init", "ts_event", "timestamp"):
        raw = trade.get(candidate)
        if raw is None:
            continue
        parsed = pd.to_datetime(raw, utc=True, errors="coerce")
        if not pd.isna(parsed):
            return parsed.to_pydatetime()
    return datetime.now(UTC)


def _account_returns_series(account_df: pd.DataFrame) -> pd.Series:
    if "returns" not in account_df.columns:
        return pd.Series(dtype=float)

    returns = pd.to_numeric(account_df["returns"], errors="coerce")
    if "timestamp" not in account_df.columns:
        return returns.dropna()

    timestamps = pd.to_datetime(account_df["timestamp"], utc=True, errors="coerce")
    frame = pd.DataFrame({"timestamp": timestamps, "returns": returns}).dropna()
    if frame.empty:
        return pd.Series(dtype=float)
    return pd.Series(frame["returns"].values, index=frame["timestamp"]).sort_index()
