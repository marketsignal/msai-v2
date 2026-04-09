from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.services.alerting import alerting_service
from msai.services.data_sources.databento_client import DatabentoClient
from msai.services.data_sources.polygon_client import PolygonClient
from msai.services.market_data_query import MarketDataQuery
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.nautilus.instrument_service import (
    NautilusInstrumentService,
    ResolvedInstrumentDefinition,
)
from msai.services.parquet_store import ParquetStore


@dataclass(slots=True, frozen=True)
class HistoricalIngestPlan:
    asset_class: str
    provider: str
    dataset: str | None
    schema: str


@dataclass(slots=True, frozen=True)
class HistoricalIngestTarget:
    requested_symbol: str
    raw_symbol: str
    instrument_id: str | None


class DataIngestionService:
    def __init__(
        self,
        parquet_store: ParquetStore,
        *,
        polygon: PolygonClient | None = None,
        databento: DatabentoClient | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.parquet_store = parquet_store
        self.polygon = polygon or PolygonClient()
        self.databento = databento or DatabentoClient()
        self.instrument_service = NautilusInstrumentService(databento=self.databento)
        self.session_factory = session_factory or async_session_factory
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
        plan = self._resolve_plan(
            asset_class=asset_class,
            provider=provider,
            dataset=dataset,
            schema=schema,
        )
        definitions = await self._ingest_definitions(
            plan=plan,
            requested_symbols=symbols,
            start=start,
            end=end,
        )
        targets = _build_ingest_targets(symbols, definitions)
        ingested: dict[str, dict[str, object]] = {}
        empty_symbols: list[str] = []
        for target in targets:
            frame = await self._fetch_bars(plan, target.raw_symbol, start, end)
            frame = _normalize_bars_frame(frame)
            stats = _frame_stats(frame)
            written_paths = self.parquet_store.write_bars(asset_class, target.raw_symbol, frame)
            if not written_paths:
                empty_symbols.append(target.raw_symbol)
            ingested[target.raw_symbol] = {
                "requested_symbol": target.requested_symbol,
                "raw_symbol": target.raw_symbol,
                "instrument_id": target.instrument_id,
                "bars": int(len(frame)),
                "files_written": int(len(written_paths)),
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
            "instrument_ids": [target.instrument_id for target in targets if target.instrument_id],
            "start": start,
            "end": end,
            "ingested": ingested,
            "empty_symbols": empty_symbols,
        }
        ensure_catalog_data(
            definitions=definitions,
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
    ) -> dict:
        yesterday = date.today() - timedelta(days=1)
        start = yesterday.isoformat()
        end = date.today().isoformat()
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
        payload: dict
        if self.status_file.exists():
            payload = json.loads(self.status_file.read_text())
        else:
            payload = {"last_run_at": None, "recent_runs": []}
        payload.setdefault("recent_runs", [])
        payload["storage_stats"] = MarketDataQuery(settings.data_root).get_storage_stats()
        return payload

    def _write_status(self, payload: dict) -> None:
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

    async def _ingest_definitions(
        self,
        *,
        plan: HistoricalIngestPlan,
        requested_symbols: list[str],
        start: str,
        end: str,
    ) -> list[ResolvedInstrumentDefinition]:
        if plan.provider != "databento" or plan.dataset is None:
            return []

        async with self.session_factory() as session:
            definitions = await self.instrument_service.ingest_databento_definitions(
                session,
                requested_symbols,
                dataset=plan.dataset,
                start=start,
                end=end,
            )
            await session.commit()
        return definitions

    async def _fetch_bars(
        self,
        plan: HistoricalIngestPlan,
        symbol: str,
        start: str,
        end: str,
    ) -> pd.DataFrame:
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
    _ = ctx
    service = DataIngestionService(ParquetStore(settings.data_root))
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
    except Exception as exc:
        alerting_service.send_alert(
            "error",
            "Market-data ingest failed",
            (
                f"asset_class={asset_class} provider={provider} dataset={dataset} "
                f"symbols={','.join(symbols)} start={start} end={end} error={exc}"
            ),
        )
        raise


def _default_databento_dataset(asset_class: str) -> str | None:
    if asset_class == "equities":
        return settings.databento_equities_dataset
    if asset_class == "futures":
        return settings.databento_futures_dataset
    return None


def _polygon_timespan_for_schema(schema: str) -> str:
    return {
        "ohlcv-1s": "minute",
        "ohlcv-1m": "minute",
        "ohlcv-1h": "hour",
        "ohlcv-1d": "day",
    }.get(schema, "minute")


def _build_ingest_targets(
    requested_symbols: list[str],
    definitions: list[ResolvedInstrumentDefinition],
) -> list[HistoricalIngestTarget]:
    definitions_by_symbol = {definition.raw_symbol: definition for definition in definitions}
    definitions_by_requested_symbol = {
        str(definition.contract_details.get("requested_symbol")): definition
        for definition in definitions
        if definition.contract_details and definition.contract_details.get("requested_symbol")
    }
    targets: list[HistoricalIngestTarget] = []
    seen_storage_symbols: set[str] = set()

    for requested_symbol in requested_symbols:
        requested_raw_symbol = _raw_symbol_from_request(requested_symbol)
        definition = definitions_by_requested_symbol.get(requested_raw_symbol) or definitions_by_symbol.get(
            requested_raw_symbol
        )
        storage_symbol = definition.raw_symbol if definition is not None else requested_raw_symbol
        if storage_symbol in seen_storage_symbols:
            continue
        seen_storage_symbols.add(storage_symbol)
        targets.append(
            HistoricalIngestTarget(
                requested_symbol=requested_symbol,
                raw_symbol=storage_symbol,
                instrument_id=definition.instrument_id if definition is not None else None,
            )
        )

    return targets


def _raw_symbol_from_request(requested_symbol: str) -> str:
    value = requested_symbol.strip()
    if not value:
        raise ValueError("Symbol cannot be empty")
    if _DATABENTO_CONTINUOUS_SYMBOL.match(value):
        return value
    if "." in value:
        return value.split(".", 1)[0]
    return value


_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")


def _normalize_bars_frame(frame: pd.DataFrame) -> pd.DataFrame:
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
