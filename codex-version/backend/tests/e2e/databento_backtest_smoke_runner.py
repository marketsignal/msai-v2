from __future__ import annotations

import asyncio
import os
from pathlib import Path

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.services.data_ingestion import DataIngestionService
from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.nautilus.strategy_config import prepare_backtest_strategy_config
from msai.services.parquet_store import ParquetStore


async def main() -> None:
    data_root = os.environ.get("RESEARCH_E2E_DATA_ROOT")
    if not data_root:
        raise RuntimeError("RESEARCH_E2E_DATA_ROOT is required")

    settings.data_root = Path(data_root)

    service = DataIngestionService(ParquetStore(settings.data_root))
    ingest_result = await service.ingest_historical(
        "equities",
        ["AAPL"],
        "2026-03-31",
        "2026-04-03",
        provider="databento",
        schema="ohlcv-1m",
    )
    if int(ingest_result["ingested"]["AAPL"]["bars"]) <= 0:
        raise RuntimeError("Databento ingestion returned no AAPL bars")

    async with async_session_factory() as session:
        definitions = await instrument_service.ensure_backtest_definitions(session, ["AAPL"])
    instrument_ids = ensure_catalog_data(
        definitions=definitions,
        raw_parquet_root=settings.parquet_root,
        catalog_root=settings.nautilus_catalog_root,
        asset_class="equities",
    )
    if instrument_ids != ["AAPL.EQUS"]:
        raise RuntimeError(f"Unexpected instrument IDs: {instrument_ids}")

    strategy_path = (
        Path(__file__).resolve().parents[3]
        / "strategies"
        / "example"
        / "mean_reversion.py"
    )
    config = prepare_backtest_strategy_config({}, instrument_ids)
    result = BacktestRunner().run(
        strategy_path=str(strategy_path),
        config=config,
        instruments=instrument_ids,
        start_date="2026-03-31",
        end_date="2026-04-03",
        data_path=settings.nautilus_catalog_root,
        timeout_seconds=5 * 60,
    )

    required_metrics = {"sharpe", "sortino", "max_drawdown", "total_return", "win_rate"}
    if not result.metrics or not required_metrics.issubset(result.metrics):
        raise RuntimeError(f"Backtest metrics were incomplete: {result.metrics}")


if __name__ == "__main__":
    asyncio.run(main())
