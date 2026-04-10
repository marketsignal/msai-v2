"""Nightly data ingestion — arq cron job.

Runs at 05:00 UTC (≈ 1:00 AM ET during EDT).
Fetches yesterday's bars for the default symbol list via
the existing DataIngestionService.ingest_daily() method.
"""

from __future__ import annotations

from typing import Any

from msai.core.config import settings
from msai.core.logging import get_logger

log = get_logger(__name__)

# Top liquid names for nightly incremental update.
_DEFAULT_STOCK_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "SPY", "QQQ", "IWM",
]


async def run_nightly_ingest(ctx: dict[str, Any]) -> dict[str, int]:
    """Fetch yesterday's bars for the default symbol list."""
    from msai.services.data_ingestion import DataIngestionService
    from msai.services.data_sources.databento_client import DatabentoClient
    from msai.services.data_sources.polygon_client import PolygonClient
    from msai.services.parquet_store import ParquetStore

    store = ParquetStore(str(settings.parquet_root))
    polygon = PolygonClient(settings.polygon_api_key) if settings.polygon_api_key else None
    databento = (
        DatabentoClient(settings.databento_api_key) if settings.databento_api_key else None
    )

    svc = DataIngestionService(store, polygon=polygon, databento=databento)
    result = await svc.ingest_daily(asset_class="stocks", symbols=_DEFAULT_STOCK_SYMBOLS)
    log.info("nightly_ingest_complete", result=result)
    return result
