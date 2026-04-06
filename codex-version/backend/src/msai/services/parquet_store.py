from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from msai.core.data_integrity import atomic_write_parquet, dedup_bars


class ParquetStore:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.parquet_root = data_root / "parquet"
        self.manifest_path = self.parquet_root / "manifest.json"

    def write_bars(self, asset_class: str, symbol: str, df: pd.DataFrame) -> list[str]:
        if df.empty:
            return []

        bars = df.copy()
        bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
        bars = bars.sort_values("timestamp")

        written_paths: list[str] = []
        updates: dict[str, str] = {}

        periods = bars["timestamp"].dt.to_period("M")
        for period, month_df in bars.groupby(periods):
            year = f"{period.year:04d}"
            month = f"{period.month:02d}"
            target = self.parquet_root / asset_class / symbol / year / f"{month}.parquet"

            merged = month_df
            if target.exists():
                existing = pq.read_table(target).to_pandas()
                merged = pd.concat([existing, month_df], ignore_index=True)
            merged = dedup_bars(merged)
            merged = merged.sort_values("timestamp")

            table = pa.Table.from_pandas(merged, preserve_index=False)
            checksum = atomic_write_parquet(table, target)
            key = str(target.relative_to(self.parquet_root))
            updates[key] = checksum
            written_paths.append(str(target))

        self._update_manifest(updates)
        return written_paths

    def read_bars(
        self,
        asset_class: str,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        symbol_root = self.parquet_root / asset_class / symbol
        if not symbol_root.exists():
            return pd.DataFrame()

        files = sorted(symbol_root.rglob("*.parquet"))
        if not files:
            return pd.DataFrame()

        frames = [pq.read_table(file).to_pandas() for file in files]
        data = pd.concat(frames, ignore_index=True)
        data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True)

        if start is not None:
            data = data[data["timestamp"] >= pd.Timestamp(start, tz="UTC")]
        if end is not None:
            data = data[data["timestamp"] <= pd.Timestamp(end, tz="UTC")]

        return data.sort_values("timestamp")

    def list_symbols(self, asset_class: str) -> list[str]:
        root = self.parquet_root / asset_class
        if not root.exists():
            return []
        return sorted(path.name for path in root.iterdir() if path.is_dir())

    def _update_manifest(self, updates: dict[str, str]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, str]
        if self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text())
        else:
            manifest = {}
        manifest.update(updates)
        self.manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
