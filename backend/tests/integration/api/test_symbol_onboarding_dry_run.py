"""Tests for POST /api/v1/symbols/onboard/dry-run."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from msai.api.symbol_onboarding import router as symbol_onboarding_router


def _make_client() -> TestClient:
    """Build a test app that mounts only the symbol-onboarding router.

    T11 wires this into ``main.py``; until then we mount it locally so this
    test does not pull in the full app's auth/middleware stack.
    """
    app = FastAPI()
    # Override the auth dependency at the app boundary so the test does not
    # need a real Entra-ID JWT or X-API-Key.
    from msai.core.auth import get_current_user

    async def _stub_user() -> dict[str, str]:
        return {"sub": "test-user", "email": "test@example.com"}

    app.dependency_overrides[get_current_user] = _stub_user
    app.include_router(symbol_onboarding_router)
    return TestClient(app)


def _fake_estimate_high_confidence():
    from msai.services.symbol_onboarding.cost_estimator import (
        CostEstimate,
        CostLine,
    )

    return CostEstimate(
        total_usd=0.42,
        symbol_count=1,
        breakdown=[CostLine("SPY", "equity", "XNAS.ITCH", 0.42)],
        confidence="high",
        basis="databento.metadata.get_cost (1m OHLCV)",
    )


def test_dry_run_happy_path() -> None:
    client = _make_client()
    body = {
        "watchlist_name": "core",
        "symbols": [
            {
                "symbol": "SPY",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
        ],
    }
    fake_estimate = AsyncMock(return_value=_fake_estimate_high_confidence())
    with patch("msai.api.symbol_onboarding.estimate_cost", new=fake_estimate):
        resp = client.post("/api/v1/symbols/onboard/dry-run", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["estimate_confidence"] == "high"
    assert data["symbol_count"] == 1


def test_dry_run_rejects_101_symbol_batch() -> None:
    client = _make_client()
    body = {
        "watchlist_name": "too-big",
        "symbols": [
            {
                "symbol": f"SYM{i:03d}",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
            for i in range(101)
        ],
    }
    resp = client.post("/api/v1/symbols/onboard/dry-run", json=body)
    assert resp.status_code == 422
    assert "symbols" in resp.text
