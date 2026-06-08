"""Window-translation semantics for ``DataIngestionService``.

The operator-facing ``end`` of ``ingest_historical`` is INCLUSIVE (matches
``compute_coverage``'s closed ``[start, end]`` window). The provider boundary
``_fetch_bars`` translates that to each provider's native convention:

- Databento ``get_range`` is end-EXCLUSIVE (SDK ``timeseries.py:68``), so the
  Databento branch fetches ``[start, end + 1d)``.
- Polygon ``/v2/aggs`` is end-INCLUSIVE, so the Polygon branch passes ``end``
  through unchanged.

These tests run the REAL ``ingest_historical`` against stub provider clients
that capture the ``(start, end)`` they receive. Side effects are isolated:
``ParquetStore`` writes to a tmp dir with no partition-index callback,
``ensure_catalog_data`` is monkeypatched to a no-op recording stub, and
``settings.data_root`` is redirected to the sandbox.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from msai.services.data_ingestion import DataIngestionService
from msai.services.parquet_store import ParquetStore


class _CapturingClient:
    """Stub provider client that records ``fetch_bars`` window args.

    Returns a 1-row valid OHLCV frame so ``ingest_historical`` does not trip
    its all-empty ``RuntimeError`` guard (unless ``empty=True``, used to
    exercise that guard explicitly).
    """

    def __init__(self, *, empty: bool = False) -> None:
        self.calls: list[dict[str, str]] = []
        self._empty = empty

    async def fetch_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        dataset: str | None = None,
        schema: str | None = None,
        timespan: str | None = None,
    ) -> pd.DataFrame:
        self.calls.append({"symbol": symbol, "start": start, "end": end})
        if self._empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        return pd.DataFrame(
            {
                "timestamp": ["2024-06-03T13:31:00Z"],
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.5],
                "volume": [1000],
            }
        )


@pytest.fixture
def isolated_service(tmp_path, monkeypatch):
    """Build a ``DataIngestionService`` with capturing stubs + isolated I/O.

    Returns ``(service, databento_stub, polygon_stub, catalog_calls)``.
    """
    catalog_calls: list[list[str]] = []

    def _record_catalog(*, symbols, raw_parquet_root, catalog_root, asset_class):
        catalog_calls.append(list(symbols))

    monkeypatch.setattr("msai.services.data_ingestion.ensure_catalog_data", _record_catalog)
    monkeypatch.setattr("msai.core.config.settings.data_root", tmp_path, raising=True)

    databento_stub = _CapturingClient()
    polygon_stub = _CapturingClient()
    service = DataIngestionService(
        ParquetStore(str(tmp_path / "parquet")),
        databento=databento_stub,  # type: ignore[arg-type]
        polygon=polygon_stub,  # type: ignore[arg-type]
    )
    return service, databento_stub, polygon_stub, catalog_calls


@pytest.mark.asyncio
async def test_ingest_historical_databento_requests_exclusive_end_plus_one(
    isolated_service,
) -> None:
    service, databento_stub, _polygon_stub, catalog_calls = isolated_service

    payload = await service.ingest_historical("equities", ["AAPL"], "2024-12-02", "2024-12-31")

    assert databento_stub.calls == [{"symbol": "AAPL", "start": "2024-12-02", "end": "2025-01-01"}]
    # The status payload records the OPERATOR (inclusive) window — never the
    # translated provider end. compute_coverage judges this same closed
    # window; leaking 2025-01-01 here would re-introduce the original
    # phantom-gap bug through the reporting side.
    assert payload["start"] == "2024-12-02"
    assert payload["end"] == "2024-12-31"
    # Cheap interaction pin: catalog sync was invoked with the ingested symbol.
    assert catalog_calls == [["AAPL"]]


@pytest.mark.asyncio
async def test_ingest_historical_polygon_receives_end_verbatim(
    isolated_service,
) -> None:
    service, _databento_stub, polygon_stub, _catalog_calls = isolated_service

    await service.ingest_historical(
        "equities", ["AAPL"], "2024-12-02", "2024-12-31", provider="polygon"
    )

    assert polygon_stub.calls == [{"symbol": "AAPL", "start": "2024-12-02", "end": "2024-12-31"}]


@pytest.mark.asyncio
async def test_ingest_daily_databento_net_window_unchanged(
    isolated_service,
) -> None:

    service, databento_stub, _polygon_stub, _catalog_calls = isolated_service

    await service.ingest_daily("equities", ["AAPL"], target_date=date(2024, 12, 30))

    # Net Databento window is unchanged from pre-fix: [2024-12-30, 2024-12-31).
    # Pins against a double-`+1` (which would yield end=2025-01-01).
    assert databento_stub.calls == [{"symbol": "AAPL", "start": "2024-12-30", "end": "2024-12-31"}]


@pytest.mark.asyncio
async def test_ingest_daily_polygon_no_double_fetch(
    isolated_service,
) -> None:

    service, _databento_stub, polygon_stub, _catalog_calls = isolated_service

    await service.ingest_daily(
        "equities", ["AAPL"], target_date=date(2024, 12, 30), provider="polygon"
    )

    # Polygon receives end == session date (pre-fix received 2024-12-31).
    assert polygon_stub.calls == [{"symbol": "AAPL", "start": "2024-12-30", "end": "2024-12-30"}]


@pytest.mark.asyncio
async def test_ingest_historical_end_before_start_raises_existing_all_empty_error(
    isolated_service,
) -> None:
    # Translation is unconditional: an end-before-start window still gets the
    # Databento +1 (stub receives end="2024-01-02"). The provider stub returns
    # an empty frame, so the EXISTING all-empty RuntimeError fires — the `+1`
    # adds no new validation surface.
    service, databento_stub, _polygon_stub, _catalog_calls = isolated_service
    databento_stub._empty = True  # noqa: SLF001 — test's own stub class

    with pytest.raises(RuntimeError, match="No historical data returned"):
        await service.ingest_historical("equities", ["AAPL"], "2024-12-31", "2024-01-01")

    assert databento_stub.calls == [{"symbol": "AAPL", "start": "2024-12-31", "end": "2024-01-02"}]


@pytest.mark.asyncio
async def test_ingest_historical_malformed_end_raises_actionable_error(
    isolated_service,
) -> None:
    # The CLI passes raw operator strings; the first parse point on the
    # Databento branch must fail with an actionable message naming the bad
    # input, not a bare "Invalid isoformat string" from translation
    # arithmetic.
    service, databento_stub, _polygon_stub, _catalog_calls = isolated_service

    with pytest.raises(ValueError, match=r"end date must be ISO YYYY-MM-DD, got '2024/12/31'"):
        await service.ingest_historical("equities", ["AAPL"], "2024-12-02", "2024/12/31")

    # Failed before any provider call.
    assert databento_stub.calls == []
