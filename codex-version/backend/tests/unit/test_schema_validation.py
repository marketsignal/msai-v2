from __future__ import annotations

import pytest
from pydantic import ValidationError

from msai.schemas.backtest import BacktestRunRequest, MarketDataIngestRequest
from msai.schemas.live import LiveStartRequest


def test_live_start_requires_non_empty_instruments() -> None:
    with pytest.raises(ValidationError):
        LiveStartRequest(strategy_id="s1", config={}, instruments=[])


def test_backtest_run_requires_non_empty_instruments() -> None:
    with pytest.raises(ValidationError):
        BacktestRunRequest(
            strategy_id="s1",
            config={},
            instruments=[],
            start_date="2024-01-01",
            end_date="2024-01-02",
        )


def test_market_data_ingest_requires_non_empty_symbols() -> None:
    with pytest.raises(ValidationError):
        MarketDataIngestRequest(
            asset_class="equities",
            symbols=[],
            start="2024-01-01",
            end="2024-01-02",
        )
