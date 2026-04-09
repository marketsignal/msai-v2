from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
from nautilus_trader.model.instruments import Instrument

from msai.core.config import settings


class DatabentoClient:
    def __init__(self, api_key: str | None = None, dataset: str | None = None) -> None:
        self.api_key = api_key or settings.databento_api_key
        self.dataset = dataset or settings.databento_futures_dataset

    async def fetch_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        dataset: str | None = None,
        schema: str | None = None,
    ) -> pd.DataFrame:
        if not self.api_key:
            raise RuntimeError("DATABENTO_API_KEY is not configured")

        import databento as db

        resolved_dataset = dataset or self.dataset
        resolved_schema = schema or settings.databento_default_schema
        client = db.Historical(key=self.api_key)
        try:
            data = client.timeseries.get_range(
                dataset=resolved_dataset,
                schema=resolved_schema,
                symbols=[symbol],
                start=start,
                end=end,
                stype_in=_databento_stype_in(symbol),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Databento historical request failed for {symbol} "
                f"(dataset={resolved_dataset}, schema={resolved_schema}): {exc}"
            ) from exc

        df = data.to_df().reset_index()
        if df.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        if "ts_event" in df.columns:
            df = df.rename(columns={"ts_event": "timestamp"})
        if "volume" not in df.columns and "size" in df.columns:
            df = df.rename(columns={"size": "volume"})
        required = ["timestamp", "open", "high", "low", "close", "volume"]
        missing = [col for col in required if col not in df.columns]
        if missing:
            raise RuntimeError(f"Databento response missing columns: {missing}")
        return df[required]

    async def fetch_definition_instruments(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        dataset: str,
        target_path: Path,
    ) -> list[Instrument]:
        if not self.api_key:
            raise RuntimeError("DATABENTO_API_KEY is not configured")

        import databento as db

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()
        client = db.Historical(key=self.api_key)
        try:
            client.timeseries.get_range(
                dataset=dataset,
                schema="definition",
                symbols=[symbol],
                start=start,
                end=end,
                stype_in=_databento_stype_in(symbol),
                stype_out="instrument_id",
                path=target_path,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Databento definition request failed for {symbol} "
                f"(dataset={dataset}): {exc}"
            ) from exc

        loader = DatabentoDataLoader()
        instruments = loader.from_dbn_file(target_path, as_legacy_cython=False)
        return list(instruments)


_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")


def _databento_stype_in(symbol: str) -> str:
    return "continuous" if _DATABENTO_CONTINUOUS_SYMBOL.match(symbol) else "raw_symbol"
