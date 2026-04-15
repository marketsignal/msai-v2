"""Data ingestion orchestrator for MSAI v2.

Coordinates fetching market data from external sources (Polygon, Databento)
and writing it to the local Parquet store.  Supports both bulk historical
downloads and incremental daily updates.

Ported from Codex version: uses a plan-based approach where Databento is
the primary provider for equities and futures, with Polygon as fallback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.data_sources.polygon_client import PolygonClient
from msai.services.market_data_query import MarketDataQuery
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.parquet_store import ParquetStore

log = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class HistoricalIngestPlan:
    """Resolved ingestion plan describing provider, dataset, and schema."""

    asset_class: str
    provider: str
    dataset: str | None
    schema: str


@dataclass(slots=True, frozen=True)
class HistoricalIngestTarget:
    """A single symbol to ingest with its resolved raw and instrument IDs."""

    requested_symbol: str
    raw_symbol: str
    instrument_id: str | None


class DataIngestionService:
    """Orchestrates data fetching from external APIs and writing to Parquet.

    Uses a plan-based approach: ``_resolve_plan()`` determines the correct
    provider (Databento or Polygon) and dataset for a given asset class.
    Databento is the primary provider for equities and futures.
    """

    def __init__(
        self,
        parquet_store: ParquetStore,
        *,
        polygon: PolygonClient | None = None,
        databento: DatabentoClient | None = None,
    ) -> None:
        self.parquet_store = parquet_store
        self.polygon = polygon or PolygonClient(settings.polygon_api_key)
        self.databento = databento or DatabentoClient()
        self.status_file = settings.data_root / "ingestion_status.json"

    async def ingest_historical(
        self,
        asset_class: str,
        symbols: list[str],
        start: str,
        end: str,
        *,
        provider: str = "auto",
        dataset: str | None = None,
        schema: str | None = None,
    ) -> dict:
        """Bulk download historical data for the given symbols.

        Routes each symbol to the appropriate data source based on the
        resolved plan, fetches the bars, normalizes them, and writes to
        the Parquet store.

        Args:
            asset_class: Asset class name (``"stocks"``, ``"equities"``,
                ``"futures"``, etc.). ``"stocks"`` is mapped to
                ``"equities"`` internally for Databento routing.
            symbols: List of ticker symbols to ingest.
            start: ISO-8601 start date (``"YYYY-MM-DD"``).
            end: ISO-8601 end date (``"YYYY-MM-DD"``).
            provider: Data provider (``"auto"``, ``"databento"``, or
                ``"polygon"``). Default ``"auto"`` routes equities and
                futures to Databento with Polygon as fallback.
            dataset: Override the default Databento dataset.
            schema: Override the default Databento schema.

        Returns:
            Detailed payload with per-symbol ingestion results.
        """
        # Map "stocks" -> "equities" for Databento routing, but keep
        # the original asset_class for Parquet storage paths.
        normalized_class = _normalize_asset_class(asset_class)
        plan = self._resolve_plan(
            asset_class=normalized_class,
            provider=provider,
            dataset=dataset,
            schema=schema,
        )
        targets = _build_ingest_targets(symbols)
        ingested: dict[str, dict[str, object]] = {}
        empty_symbols: list[str] = []

        for target in targets:
            frame = await self._fetch_bars(plan, target.raw_symbol, start, end)
            frame = _normalize_bars_frame(frame)
            stats = _frame_stats(frame)
            # Use the original asset_class for Parquet storage so existing
            # paths ("stocks/AAPL/...") are preserved.
            written = self.parquet_store.write_bars(asset_class, target.raw_symbol, frame)
            if not written:
                empty_symbols.append(target.raw_symbol)
            ingested[target.raw_symbol] = {
                "requested_symbol": target.requested_symbol,
                "raw_symbol": target.raw_symbol,
                "instrument_id": target.instrument_id,
                "bars": int(len(frame)),
                "first_timestamp": stats["first_timestamp"],
                "last_timestamp": stats["last_timestamp"],
                "duplicates_dropped": stats["duplicates_dropped"],
            }

        if all(details["bars"] == 0 for details in ingested.values()):
            raise RuntimeError(
                f"No historical data returned for {asset_class} symbols {symbols} "
                f"using provider={plan.provider}, dataset={plan.dataset}, schema={plan.schema}"
            )

        payload = {
            "asset_class": asset_class,
            "provider": plan.provider,
            "dataset": plan.dataset,
            "schema": plan.schema,
            "requested_symbols": symbols,
            "symbols": [target.raw_symbol for target in targets],
            "start": start,
            "end": end,
            "ingested": ingested,
            "empty_symbols": empty_symbols,
        }

        # Update the Nautilus catalog with the newly ingested data.
        ensure_catalog_data(
            symbols=[target.raw_symbol for target in targets],
            raw_parquet_root=settings.parquet_root,
            catalog_root=settings.nautilus_catalog_root,
            asset_class=asset_class,
        )

        self._write_status(payload)
        return payload

    async def ingest_daily(
        self,
        asset_class: str,
        symbols: list[str],
        *,
        provider: str = "auto",
        dataset: str | None = None,
        schema: str | None = None,
        target_date: date | None = None,
    ) -> dict:
        """Fetch a single trading session's data.

        ``target_date`` is the session date to fetch (caller's calendar,
        typically the scheduled-tz date post-close). This method asks
        the provider for ``[target_date, target_date + 1)`` so Databento's
        end-exclusive window yields just that session's bars — Codex iter
        3 P1.

        ``target_date`` defaults to ``yesterday`` in the process tz so
        existing CLI / manual-trigger callers preserve the "yesterday's
        session" semantics the method previously had.

        **Provider window semantics** (Codex iter 5 P2 — follow-up): the
        ``[target_date, target_date + 1)`` window is exclusive on the
        ``end`` boundary. Databento honours that. Polygon's ``/v2/aggs``
        range endpoint is end-*inclusive*, so a Polygon-routed
        ``ingest_daily(target_date=X)`` call fetches both ``X`` and
        ``X + 1``. The duplicate ``X + 1`` rows are dropped by
        ``ParquetStore`` (timestamp-keyed dedup on write), so no data
        corruption — only wasted download bandwidth. Normalising the
        per-provider semantics is tracked as a separate improvement; the
        prior codepath (``end = date.today()``) had the same Polygon
        2-day overlap, so this isn't a Phase 2 #3 regression.

        **Mixed-exchange limitation** (Codex iter 5 P2 — follow-up): a
        single ``target_date`` is applied to every asset regardless of
        exchange. Operators running mixed LSE + NYSE universes should
        either schedule separate daily ingests per exchange (requires
        multiple worker configurations) or pick a schedule after the
        latest market close. Per-exchange scheduling is out of scope
        for this port.

        Args:
            asset_class: Asset class name.
            symbols: List of ticker symbols to update.
            provider: Data provider override.
            dataset: Databento dataset override.
            schema: Databento schema override.
            target_date: Session date to ingest. Default: yesterday
                (process tz). The tz-aware scheduler wrapper passes
                ``current.date()`` in the scheduled tz so a 18:00 ET ingest
                on a UTC host fetches the just-closed US session.

        Returns:
            Detailed payload with per-symbol ingestion results.
        """
        session_date = target_date if target_date is not None else date.today() - timedelta(days=1)
        start = session_date.isoformat()
        end = (session_date + timedelta(days=1)).isoformat()
        return await self.ingest_historical(
            asset_class,
            symbols,
            start,
            end,
            provider=provider,
            dataset=dataset,
            schema=schema,
        )

    def data_status(self) -> dict:
        """Return the most recent ingestion status and storage stats."""
        payload: dict
        if self.status_file.exists():
            payload = json.loads(self.status_file.read_text())
        else:
            payload = {"last_run_at": None, "recent_runs": []}
        payload.setdefault("recent_runs", [])
        payload["storage_stats"] = MarketDataQuery(settings.data_root).get_storage_stats()
        return payload

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write_status(self, payload: dict) -> None:
        """Persist ingestion run result to the status file."""
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        status = self.data_status()
        run = {
            "recorded_at": date.today().isoformat(),
            **payload,
        }
        status["last_run_at"] = date.today().isoformat()
        status["recent_runs"] = [run, *status.get("recent_runs", [])][:20]
        self.status_file.write_text(json.dumps(status, indent=2, sort_keys=True))

    def _resolve_plan(
        self,
        *,
        asset_class: str,
        provider: str,
        dataset: str | None,
        schema: str | None,
    ) -> HistoricalIngestPlan:
        """Determine provider, dataset, and schema for the ingestion request.

        When ``provider="auto"``, equities and futures route to Databento;
        everything else falls back to Polygon.
        """
        resolved_provider = provider
        if resolved_provider == "auto":
            resolved_provider = "databento" if asset_class in {"equities", "futures"} else "polygon"

        resolved_schema = schema or settings.databento_default_schema
        if resolved_provider == "databento":
            resolved_dataset = dataset or _default_databento_dataset(asset_class)
            if resolved_dataset is None:
                raise ValueError(
                    f"No default Databento dataset configured for asset class '{asset_class}'."
                )
            return HistoricalIngestPlan(
                asset_class=asset_class,
                provider=resolved_provider,
                dataset=resolved_dataset,
                schema=resolved_schema,
            )

        if resolved_provider == "polygon":
            if asset_class == "futures":
                raise ValueError("Polygon is not configured as the futures research backbone.")
            return HistoricalIngestPlan(
                asset_class=asset_class,
                provider=resolved_provider,
                dataset=dataset,
                schema=resolved_schema,
            )

        raise ValueError(f"Unsupported ingestion provider: {provider}")

    async def _fetch_bars(
        self,
        plan: HistoricalIngestPlan,
        symbol: str,
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """Route to the correct data source and fetch bars."""
        if plan.provider == "databento":
            return await self.databento.fetch_bars(
                symbol,
                start,
                end,
                dataset=plan.dataset,
                schema=plan.schema,
            )
        return await self.polygon.fetch_bars(
            symbol,
            start,
            end,
            timespan=_polygon_timespan_for_schema(plan.schema),
        )


async def run_ingest(
    ctx: dict,
    asset_class: str,
    symbols: list[str],
    start: str,
    end: str,
    provider: str = "auto",
    dataset: str | None = None,
    schema: str | None = None,
) -> None:
    """arq-compatible entry point for data ingestion jobs.

    Instantiates the service with default clients from settings and runs
    historical ingestion. On failure, logs the error and re-raises.
    """
    _ = ctx
    service = DataIngestionService(ParquetStore(str(settings.parquet_root)))
    try:
        await service.ingest_historical(
            asset_class,
            symbols,
            start,
            end,
            provider=provider,
            dataset=dataset,
            schema=schema,
        )
    except Exception:
        log.error(
            "ingest_failed",
            asset_class=asset_class,
            provider=provider,
            dataset=dataset,
            symbols=",".join(symbols),
            start=start,
            end=end,
        )
        raise


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _normalize_asset_class(asset_class: str) -> str:
    """Map ``"stocks"`` to ``"equities"`` for Databento routing.

    This allows callers to use either name. The Parquet storage path still
    uses the original value (e.g. ``"stocks"``).
    """
    if asset_class == "stocks":
        return "equities"
    return asset_class


def _default_databento_dataset(asset_class: str) -> str | None:
    """Return the default Databento dataset for a given asset class."""
    if asset_class == "equities":
        return settings.databento_equities_dataset
    if asset_class == "futures":
        return settings.databento_futures_dataset
    return None


def _polygon_timespan_for_schema(schema: str) -> str:
    """Map a Databento-style schema name to a Polygon timespan."""
    return {
        "ohlcv-1s": "minute",
        "ohlcv-1m": "minute",
        "ohlcv-1h": "hour",
        "ohlcv-1d": "day",
    }.get(schema, "minute")


def _build_ingest_targets(
    requested_symbols: list[str],
) -> list[HistoricalIngestTarget]:
    """Build ingestion targets from requested symbols.

    Without the full instrument definition service (which requires heavy
    IB/Nautilus dependencies), we resolve targets by stripping venue
    suffixes and deduplicating.
    """
    targets: list[HistoricalIngestTarget] = []
    seen: set[str] = set()

    for requested_symbol in requested_symbols:
        raw_symbol = _raw_symbol_from_request(requested_symbol)
        if raw_symbol in seen:
            continue
        seen.add(raw_symbol)
        targets.append(
            HistoricalIngestTarget(
                requested_symbol=requested_symbol,
                raw_symbol=raw_symbol,
                instrument_id=None,
            )
        )

    return targets


_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")


def _raw_symbol_from_request(requested_symbol: str) -> str:
    """Extract the raw symbol from a request string.

    Handles Databento continuous symbols (e.g. ``"ES.c.0"``) by leaving
    them unchanged, and strips venue suffixes (e.g. ``"AAPL.SIM"`` ->
    ``"AAPL"``) for regular symbols.
    """
    value = requested_symbol.strip()
    if not value:
        raise ValueError("Symbol cannot be empty")
    if _DATABENTO_CONTINUOUS_SYMBOL.match(value):
        return value
    if "." in value:
        return value.split(".", 1)[0]
    return value


def _normalize_bars_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize and deduplicate a bars DataFrame.

    Ensures the required columns are present, casts timestamps to UTC,
    drops NaN rows, sorts by time, and removes duplicates.
    """
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise RuntimeError(f"Historical data is missing required columns: {missing}")

    if frame.empty:
        return pd.DataFrame(columns=required)

    bars = frame[required].copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    bars = bars.dropna(subset=required)
    bars = bars.sort_values("timestamp")
    bars = bars.drop_duplicates(subset=["timestamp"], keep="last")
    return bars.reset_index(drop=True)


def _frame_stats(frame: pd.DataFrame) -> dict[str, object]:
    """Compute summary statistics for a normalized bars DataFrame."""
    if frame.empty:
        return {
            "first_timestamp": None,
            "last_timestamp": None,
            "duplicates_dropped": 0,
        }
    original_len = len(frame)
    deduped_len = len(frame.drop_duplicates(subset=["timestamp"], keep="last"))
    return {
        "first_timestamp": frame["timestamp"].iloc[0].isoformat(),
        "last_timestamp": frame["timestamp"].iloc[-1].isoformat(),
        "duplicates_dropped": int(original_len - deduped_len),
    }
