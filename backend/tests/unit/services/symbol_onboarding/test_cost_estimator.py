from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from msai.schemas.symbol_onboarding import OnboardSymbolSpec
from msai.services.symbol_onboarding.cost_estimator import (
    CostEstimate,
    estimate_cost,
)
from msai.services.symbol_onboarding.manifest import ParsedManifest


def _spec(sym, ac, start, end):
    return OnboardSymbolSpec(symbol=sym, asset_class=ac, start=start, end=end)


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.metadata = MagicMock()
    client.metadata.get_cost = MagicMock(return_value=0.42)
    return client


@pytest.mark.asyncio
async def test_estimate_returns_high_confidence_on_fully_historical_equity(fake_client):
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[_spec("SPY", "equity", date(2023, 1, 1), date(2024, 12, 31))],
    )
    result = await estimate_cost(manifest, client=fake_client, today=date(2026, 4, 24))
    assert isinstance(result, CostEstimate)
    assert result.total_usd == pytest.approx(0.42)
    assert result.confidence == "high"
    assert len(result.breakdown) == 1
    assert result.breakdown[0].symbol == "SPY"


@pytest.mark.asyncio
async def test_estimate_confidence_medium_when_end_touches_yesterday(fake_client):
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[_spec("SPY", "equity", date(2024, 1, 1), date(2026, 4, 23))],
    )
    result = await estimate_cost(manifest, client=fake_client, today=date(2026, 4, 24))
    assert result.confidence == "medium"


@pytest.mark.asyncio
async def test_estimate_confidence_medium_on_continuous_futures(fake_client):
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[_spec("ES.n.0", "futures", date(2023, 1, 1), date(2024, 12, 31))],
    )
    result = await estimate_cost(manifest, client=fake_client, today=date(2026, 4, 24))
    assert result.confidence == "medium"


@pytest.mark.asyncio
async def test_estimate_batches_symbols_per_dataset(fake_client):
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[
            _spec("SPY", "equity", date(2024, 1, 1), date(2024, 12, 31)),
            _spec("AAPL", "equity", date(2024, 1, 1), date(2024, 12, 31)),
        ],
    )
    await estimate_cost(manifest, client=fake_client, today=date(2026, 4, 24))
    assert fake_client.metadata.get_cost.call_count == 1


@pytest.mark.asyncio
async def test_estimate_returns_low_confidence_on_upstream_failure(fake_client):
    fake_client.metadata.get_cost.side_effect = RuntimeError("auth failed")
    manifest = ParsedManifest(
        watchlist_name="m",
        symbols=[_spec("SPY", "equity", date(2023, 1, 1), date(2024, 12, 31))],
    )
    result = await estimate_cost(manifest, client=fake_client, today=date(2026, 4, 24))
    assert result.total_usd == 0.0
    assert result.confidence == "low"
    assert "unavailable" in result.basis.lower()
