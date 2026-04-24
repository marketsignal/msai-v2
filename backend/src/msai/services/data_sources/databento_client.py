"""Databento API client for fetching OHLCV bar data.

Uses the Databento Python SDK to retrieve historical minute bars for
equities and futures contracts.  Returns normalized DataFrames compatible
with the ParquetStore write format.

Supports configurable dataset and schema parameters so the same client
can serve both equities (EQUS.MINI) and futures (GLBX.MDP3).
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import pandas as pd
from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from msai.core.config import settings
from msai.core.logging import get_logger
from msai.services.data_sources.databento_errors import DatabentoError
from msai.services.observability.trading_metrics import DATABENTO_API_CALLS_TOTAL

if TYPE_CHECKING:
    from pathlib import Path

    from nautilus_trader.model.instruments import Instrument

log = get_logger(__name__)


class AmbiguousDatabentoSymbolError(DatabentoError):
    """Databento returned multiple distinct instruments for a single symbol request.

    Carries a ``candidates`` list (alias_string, raw_symbol, asset_class,
    dataset per candidate) that the API layer surfaces to the operator as
    HTTP 422. Operator retries with ``exact_ids={SYMBOL: alias_string}``
    from the candidates — fetch_definition_instruments' ``exact_id`` kwarg
    pre-filters on the second call.
    """

    def __init__(
        self,
        *,
        symbol: str,
        candidates: list[dict[str, str]],
        dataset: str | None = None,
    ) -> None:
        self.symbol = symbol
        self.candidates = candidates
        super().__init__(
            f"Databento returned {len(candidates)} distinct instruments for {symbol!r}",
            dataset=dataset,
        )


# Databento's Historical REST rate limits are not publicly documented.
# The SDK auto-retries only on batch.download, NOT on
# timeseries.get_range — wrap the latter here.
#
# Policy: 3 attempts, exponential backoff 1s → 3s → 9s (~13s max). Retries
# on 429 and 5xx only — 401/403 fail fast.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    """True if the Databento SDK error is 429 or 5xx."""
    from databento.common.error import BentoClientError, BentoServerError

    if isinstance(exc, (BentoClientError, BentoServerError)):
        return getattr(exc, "http_status", None) in _RETRYABLE_STATUSES
    return False


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
        exact_id: str | None = None,
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
        from databento.common.error import BentoClientError, BentoServerError

        from msai.services.data_sources.databento_errors import (
            DatabentoRateLimitedError,
            DatabentoUnauthorizedError,
            DatabentoUpstreamError,
        )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic rename preserves prior good definition file if SDK fails —
        # download to a sibling ``.tmp`` first and rename on success.
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        client = db.Historical(key=self.api_key)

        def _sync_download() -> None:
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

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=1, max=9),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    # Run sync SDK call off the event loop.
                    await asyncio.to_thread(_sync_download)
        except BentoClientError as exc:
            tmp_path.unlink(missing_ok=True)
            if exc.http_status in (401, 403):
                DATABENTO_API_CALLS_TOTAL.labels(
                    endpoint="definition", outcome="unauthorized"
                ).inc()
                raise DatabentoUnauthorizedError(
                    f"Databento unauthorized for {symbol} on {dataset}: {exc}",
                    http_status=exc.http_status,
                    dataset=dataset,
                ) from exc
            if exc.http_status == 429:
                DATABENTO_API_CALLS_TOTAL.labels(
                    endpoint="definition", outcome="rate_limited_failed"
                ).inc()
                raise DatabentoRateLimitedError(
                    f"Databento rate-limited after retries for {symbol} on {dataset}: {exc}",
                    http_status=exc.http_status,
                    dataset=dataset,
                ) from exc
            DATABENTO_API_CALLS_TOTAL.labels(endpoint="definition", outcome="upstream_error").inc()
            raise DatabentoUpstreamError(
                f"Databento 4xx for {symbol} on {dataset}: {exc}",
                http_status=exc.http_status,
                dataset=dataset,
            ) from exc
        except BentoServerError as exc:
            tmp_path.unlink(missing_ok=True)
            DATABENTO_API_CALLS_TOTAL.labels(endpoint="definition", outcome="upstream_error").inc()
            raise DatabentoUpstreamError(
                f"Databento 5xx for {symbol} on {dataset}: {exc}",
                http_status=exc.http_status,
                dataset=dataset,
            ) from exc
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            DATABENTO_API_CALLS_TOTAL.labels(endpoint="definition", outcome="upstream_error").inc()
            raise DatabentoUpstreamError(
                f"Databento unexpected error for {symbol} on {dataset}: {exc}",
                dataset=dataset,
            ) from exc

        tmp_path.replace(target_path)
        DATABENTO_API_CALLS_TOTAL.labels(endpoint="definition", outcome="success").inc()

        # `use_exchange_as_venue=True` is a per-call kwarg of
        # `DatabentoDataLoader.from_dbn_file` (see
        # `nautilus_trader/adapters/databento/loaders.py`); it is NOT a
        # constructor kwarg of `DatabentoDataLoader`.
        # Setting it ensures CME futures emit venue='CME' not 'GLBX' — keeps
        # registry canonical alias in exchange-name form.
        loader = DatabentoDataLoader()
        instruments = list(
            loader.from_dbn_file(
                target_path,
                as_legacy_cython=False,
                use_exchange_as_venue=True,
            )
        )

        # Dedup by canonical id (same instrument emitted across multiple time
        # windows appears N times with the same id.value).
        seen: dict[str, Instrument] = {}
        for inst in instruments:
            key = str(inst.id.value) if hasattr(inst, "id") else repr(inst)
            seen.setdefault(key, inst)
        distinct = list(seen.values())

        # When caller provided an exact_id (from a prior ambiguity 422's
        # candidates[]), filter to that single alias BEFORE deciding ambiguous.
        # Lets the "retry with exact_id" flow resolve cleanly on the second pass.
        if exact_id is not None:
            distinct = [i for i in distinct if str(i.id.value) == exact_id]
            if not distinct:
                raise DatabentoUpstreamError(
                    f"exact_id {exact_id!r} not in {symbol}'s candidates for {dataset}",
                    http_status=None,
                    dataset=dataset,
                )

        if len(distinct) > 1:
            candidates: list[dict[str, str]] = []
            for inst in distinct:
                candidates.append(
                    {
                        "alias_string": str(inst.id.value),
                        "raw_symbol": (
                            inst.raw_symbol.value
                            if hasattr(inst, "raw_symbol") and hasattr(inst.raw_symbol, "value")
                            else symbol
                        ),
                        "asset_class": inst.__class__.__name__,
                        "dataset": dataset,
                    }
                )
            raise AmbiguousDatabentoSymbolError(
                symbol=symbol,
                candidates=candidates,
                dataset=dataset,
            )

        return distinct


_DATABENTO_CONTINUOUS_SYMBOL = re.compile(r"^[A-Za-z0-9_/-]+\.[A-Za-z]\.\d+$")


def _databento_stype_in(symbol: str) -> str:
    """Determine the Databento symbol type input parameter.

    Continuous contract symbols (e.g. ``"ES.c.0"``) use ``"continuous"``
    while all other symbols use ``"raw_symbol"``.
    """
    return "continuous" if _DATABENTO_CONTINUOUS_SYMBOL.match(symbol) else "raw_symbol"
