"""Integration tests for POST /api/v1/symbols/onboard/dry-run endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio

from msai.api.symbol_onboarding import router as symbol_onboarding_router
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.main import app
from msai.services.symbol_onboarding.cost_estimator import CostEstimate, CostLine

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: TC002

# Wire the router for T8 testing (T11 will do this in main.py)
app.include_router(symbol_onboarding_router)


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI client with dependency overrides for testing."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async def _override_current_user() -> dict[str, Any]:
        return {"sub": "test-user", "preferred_username": "test@example.com"}

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current_user

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()


async def test_dry_run_happy_path(
    client: httpx.AsyncClient,
) -> None:
    """POST /dry-run with 1 symbol returns estimate with high confidence."""
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
    fake_estimate = _fake_estimate_high_confidence()
    with patch(
        "msai.api.symbol_onboarding.estimate_cost",
        new_callable=AsyncMock,
        return_value=fake_estimate,
    ):
        resp = await client.post(
            "/api/v1/symbols/onboard/dry-run",
            json=body,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["estimate_confidence"] == "high"
    assert data["symbol_count"] == 1
    assert data["watchlist_name"] == "core"
    assert len(data["breakdown"]) == 1
    assert data["breakdown"][0]["symbol"] == "SPY"
    assert data["breakdown"][0]["asset_class"] == "equity"


async def test_dry_run_rejects_101_symbols(
    client: httpx.AsyncClient,
) -> None:
    """POST /dry-run with >100 symbols returns 422."""
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
    resp = await client.post(
        "/api/v1/symbols/onboard/dry-run",
        json=body,
    )
    assert resp.status_code == 422
    # Pydantic error body should mention "symbols"
    assert "symbols" in resp.text


async def test_dry_run_rejects_invalid_asset_class(
    client: httpx.AsyncClient,
) -> None:
    """POST /dry-run with invalid asset_class returns 422."""
    body = {
        "watchlist_name": "bad",
        "symbols": [
            {
                "symbol": "AAPL",
                "asset_class": "invalid_class",  # type: ignore
                "start": "2024-01-01",
                "end": "2024-12-31",
            }
        ],
    }
    resp = await client.post(
        "/api/v1/symbols/onboard/dry-run",
        json=body,
    )
    assert resp.status_code == 422


async def test_dry_run_rejects_end_before_start(
    client: httpx.AsyncClient,
) -> None:
    """POST /dry-run with end < start returns 422."""
    body = {
        "watchlist_name": "bad-dates",
        "symbols": [
            {
                "symbol": "SPY",
                "asset_class": "equity",
                "start": "2024-12-31",
                "end": "2024-01-01",
            }
        ],
    }
    resp = await client.post(
        "/api/v1/symbols/onboard/dry-run",
        json=body,
    )
    assert resp.status_code == 422
    assert "end" in resp.text or "start" in resp.text


async def test_dry_run_multiple_symbols(
    client: httpx.AsyncClient,
) -> None:
    """POST /dry-run with 3 symbols returns all estimates."""
    body = {
        "watchlist_name": "multi",
        "symbols": [
            {
                "symbol": "SPY",
                "asset_class": "equity",
                "start": "2024-01-01",
                "end": "2024-12-31",
            },
            {
                "symbol": "QQQ",
                "asset_class": "equity",
                "start": "2024-06-01",
                "end": "2024-12-31",
            },
            {
                "symbol": "ES",
                "asset_class": "futures",
                "start": "2024-01-01",
                "end": "2024-03-31",
            },
        ],
    }

    def _fake_multi_estimate() -> CostEstimate:
        return CostEstimate(
            total_usd=1.50,
            symbol_count=3,
            breakdown=[
                CostLine("SPY", "equity", "XNAS.ITCH", 0.60),
                CostLine("QQQ", "equity", "XNAS.ITCH", 0.60),
                CostLine("ES", "futures", "GLBX.MDP3", 0.30),
            ],
            confidence="high",
            basis="databento.metadata.get_cost (1m OHLCV)",
        )

    with patch(
        "msai.api.symbol_onboarding.estimate_cost",
        new_callable=AsyncMock,
        return_value=_fake_multi_estimate(),
    ):
        resp = await client.post(
            "/api/v1/symbols/onboard/dry-run",
            json=body,
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol_count"] == 3
    assert len(data["breakdown"]) == 3


@pytest.mark.skip(reason="Router not wired into main.py yet (T11)")
async def test_dry_run_requires_auth(
    client: httpx.AsyncClient,
) -> None:
    """POST /dry-run without JWT returns 403 (dependency stub is in place)."""
    # Clear overrides to simulate missing JWT
    app.dependency_overrides.clear()

    async def _fail_auth() -> dict[str, Any]:
        from fastapi import HTTPException

        raise HTTPException(status_code=403)

    app.dependency_overrides[get_current_user] = _fail_auth

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
    resp = await client.post(
        "/api/v1/symbols/onboard/dry-run",
        json=body,
    )
    assert resp.status_code == 403
    app.dependency_overrides.clear()


def _fake_estimate_high_confidence() -> CostEstimate:
    """Fixture: high-confidence cost estimate for 1 SPY symbol."""
    return CostEstimate(
        total_usd=0.42,
        symbol_count=1,
        breakdown=[CostLine("SPY", "equity", "XNAS.ITCH", 0.42)],
        confidence="high",
        basis="databento.metadata.get_cost (1m OHLCV)",
    )
