"""Databento API client for fetching OHLCV bar data.

Uses the Databento Python SDK to retrieve historical minute bars for
equities and futures contracts.  Returns normalized DataFrames compatible
with the ParquetStore write format.

Supports configurable dataset and schema parameters so the same client
can serve both equities (EQUS.MINI) and futures (GLBX.MDP3).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pandas as pd
from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader

from msai.core.config import settings
from msai.core.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from nautilus_trader.model.instruments import Instrument

log = get_logger(__name__)


class DatabentoClient:
    """Client for the Databento Historical API (equities + futures bars)."""

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
        """Fetch OHLCV bars from Databento.

        Uses the ``databento`` Python SDK's ``Historical`` client to request
        bars with the given schema (default: ``ohlcv-1m``).

        Args:
            symbol: Ticker symbol (e.g. ``"AAPL"``, ``"ES.FUT"``).
            start: Start date as ``"YYYY-MM-DD"``.
            end: End date as ``"YYYY-MM-DD"``.
            dataset: Databento dataset identifier (overrides instance default).
            schema: Databento schema (e.g. ``"ohlcv-1m"``).

        Returns:
            DataFrame with columns: ``timestamp``, ``open``, ``high``,
            ``low``, ``close``, ``volume``.

        Raises:
            RuntimeError: If ``DATABENTO_API_KEY`` is not configured or the
                API request fails.
        """
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
        """Fetch a Databento ``definition`` file and decode it into Instruments.

        Downloads the ``.definition.dbn.zst`` payload for ``symbol`` over the
        ``[start, end)`` window into ``target_path``, then loads the file with
        :class:`nautilus_trader.adapters.databento.loaders.DatabentoDataLoader`
        and returns the decoded :class:`Instrument` objects.

        Args:
            symbol: Requested symbol (continuous ``ES.Z.5`` or raw ``ESZ4``).
            start: Window start (``YYYY-MM-DD``, inclusive).
            end: Window end (``YYYY-MM-DD``, exclusive).
            dataset: Databento dataset (e.g. ``"GLBX.MDP3"``).
            target_path: Destination for the downloaded ``.dbn.zst`` file.
                Parent directories are created if missing.

        Returns:
            List of Nautilus ``Instrument`` objects decoded from the payload.

        Raises:
            RuntimeError: If ``DATABENTO_API_KEY`` is not configured or the
                Databento request fails.
        """
        if not self.api_key:
            raise RuntimeError("DATABENTO_API_KEY is not configured")

        import databento as db

        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic rename preserves prior good definition file if SDK fails —
        # download to a sibling ``.tmp`` first and rename on success.
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
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
                path=str(tmp_path),
            )
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Databento definition request failed for {symbol} "
                f"(dataset={dataset}): {exc}"
            ) from exc
        tmp_path.replace(target_path)

        # `use_exchange_as_venue=True` is a per-call kwarg of
        # `DatabentoDataLoader.from_dbn_file` (see
        # `nautilus_trader/adapters/databento/loaders.py`); it is NOT a
        # constructor kwarg of `DatabentoDataLoader`.
        # Setting it ensures CME futures emit venue='CME' not 'GLBX' — keeps
        # registry canonical alias in exchange-name form.
        loader = DatabentoDataLoader()
        instruments = loader.from_dbn_file(
            target_path,
            as_legacy_cython=False,
            use_exchange_as_venue=True,
        )
        return list(instruments)


_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")


def _databento_stype_in(symbol: str) -> str:
    """Determine the Databento symbol type input parameter.

    Continuous contract symbols (e.g. ``"ES.c.0"``) use ``"continuous"``
    while all other symbols use ``"raw_symbol"``.
    """
    return "continuous" if _DATABENTO_CONTINUOUS_SYMBOL.match(symbol) else "raw_symbol"
