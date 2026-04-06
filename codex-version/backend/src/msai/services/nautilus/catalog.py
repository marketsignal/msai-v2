from __future__ import annotations

from pathlib import Path


class NautilusCatalog:
    def get_catalog(self, data_path: Path):
        try:
            from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
        except Exception as exc:
            raise RuntimeError("NautilusTrader catalog import failed") from exc
        return ParquetDataCatalog(path=str(data_path))

    def get_instruments(self, catalog: object) -> list:
        if hasattr(catalog, "instruments"):
            instruments = catalog.instruments()
            return list(instruments)
        return []
