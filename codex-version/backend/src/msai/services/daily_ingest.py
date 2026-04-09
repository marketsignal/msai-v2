from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DailyIngestRequest:
    asset_class: str
    symbols: list[str]
    provider: str
    dataset: str
    schema: str = "ohlcv-1m"


DAILY_INGEST_REQUESTS: tuple[DailyIngestRequest, ...] = (
    DailyIngestRequest(
        asset_class="equities",
        symbols=["SPY", "IWM", "DIA", "EFA", "EEM", "GLD"],
        provider="databento",
        dataset="ARCX.PILLAR",
    ),
    DailyIngestRequest(
        asset_class="equities",
        symbols=["QQQ"],
        provider="databento",
        dataset="XNAS.ITCH",
    ),
    DailyIngestRequest(
        asset_class="futures",
        symbols=["ES.v.0", "NQ.v.0", "RTY.v.0", "YM.v.0", "GC.v.0"],
        provider="databento",
        dataset="GLBX.MDP3",
    ),
)
