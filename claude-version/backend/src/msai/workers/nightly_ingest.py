"""Nightly data ingestion — arq cron job.

Runs at 05:00 UTC (approximately 1:00 AM ET during EDT).
Fetches yesterday's bars for all enabled assets in the asset universe.
Falls back to a hardcoded default list when the universe table is empty
or the database is unreachable.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from msai.core.config import settings
from msai.core.logging import get_logger

log = get_logger(__name__)

# Fallback list used only when the asset universe table is empty or the DB
# cannot be reached.  Keeps the worker functional during bootstrap.
_FALLBACK_STOCK_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "SPY", "QQQ", "IWM",
]


async def _load_targets_from_db() -> list[Any] | None:
    """Attempt to load enabled assets from the asset universe table.

    Returns a list of :class:`AssetUniverse` rows, or ``None`` if the
    database is unreachable or the table is empty.
    """
    try:
        from msai.core.database import async_session_factory
        from msai.services.asset_universe import AssetUniverseService

        service = AssetUniverseService()
        async with async_session_factory() as session:
            targets = await service.get_ingest_targets(session)
            if targets:
                return targets
            log.warning("nightly_ingest_empty_universe", fallback="default_symbols")
            return None
    except Exception as exc:
        log.warning("nightly_ingest_db_unavailable", error=str(exc), fallback="default_symbols")
        return None


async def _mark_ingested(targets: list[Any]) -> None:
    """Update last_ingested_at for all ingested assets."""
    try:
        from msai.core.database import async_session_factory
        from msai.services.asset_universe import AssetUniverseService

        service = AssetUniverseService()
        now = datetime.now(timezone.utc)
        async with async_session_factory() as session:
            for asset in targets:
                await service.mark_ingested(session, asset.id, now)
            await session.commit()
    except Exception as exc:
        log.warning("nightly_ingest_mark_failed", error=str(exc))


async def run_nightly_ingest(ctx: dict[str, Any]) -> dict[str, int]:
    """Fetch yesterday's bars for all enabled assets in the universe.

    If the asset universe table is empty or unreachable, falls back to
    the hardcoded default stock symbols so the worker remains functional.
    """
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

    # Try DB-backed universe first
    targets = await _load_targets_from_db()
    combined_result: dict[str, int] = {}

    if targets is not None:
        # Group by asset_class for efficient batching
        groups: dict[str, list[str]] = defaultdict(list)
        target_lookup: dict[str, Any] = {}
        for asset in targets:
            groups[asset.asset_class].append(asset.symbol)
            target_lookup[asset.symbol] = asset

        for asset_class, symbols in groups.items():
            result = await svc.ingest_daily(asset_class=asset_class, symbols=symbols)
            combined_result.update(result)

        # Only mark assets whose ingestion returned non-zero rows as fresh
        successful_targets = [
            asset
            for asset in targets
            if combined_result.get(asset.symbol, 0) > 0
        ]
        if successful_targets:
            await _mark_ingested(successful_targets)
        log.info("nightly_ingest_complete", source="database", result=combined_result)
    else:
        # Fallback to hardcoded list
        combined_result = await svc.ingest_daily(
            asset_class="stocks", symbols=_FALLBACK_STOCK_SYMBOLS
        )
        log.info("nightly_ingest_complete", source="fallback", result=combined_result)

    return combined_result
