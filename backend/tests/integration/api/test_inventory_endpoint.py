"""Integration tests for GET /api/v1/symbols/inventory (B3).

Validates the bulk readiness contract:

- Empty registry → empty array.
- Two registered instruments → two rows with correct symbols / asset_class.
- ``asset_class`` filter narrows the row set.
- Without ``start`` + ``end`` → ``backtest_data_available`` is null.
- ``hidden_from_inventory=True`` rows are excluded (B6a soft-delete).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from msai.api.symbol_onboarding import router as symbol_onboarding_router
from msai.core.auth import get_current_user
from msai.core.database import get_db
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _build_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()

    async def _stub_user() -> dict[str, str]:
        return {"sub": "test-user", "email": "test@example.com"}

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_current_user] = _stub_user
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(symbol_onboarding_router)
    return app


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = _build_app(session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


async def _seed_active_alias(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    raw_symbol: str,
    asset_class: str,
    provider: str = "databento",
    listing_venue: str = "XNAS",
    routing_venue: str = "XNAS",
    alias_string: str | None = None,
    venue_format: str = "exchange_name",
) -> InstrumentDefinition:
    async with session_factory() as s:
        defn = InstrumentDefinition(
            raw_symbol=raw_symbol,
            listing_venue=listing_venue,
            routing_venue=routing_venue,
            asset_class=asset_class,
            provider=provider,
            lifecycle_state="active",
        )
        s.add(defn)
        await s.flush()
        alias = InstrumentAlias(
            instrument_uid=defn.instrument_uid,
            alias_string=alias_string or f"{raw_symbol}.{listing_venue}",
            venue_format=venue_format,
            provider=provider,
            effective_from=date(2020, 1, 1),
            effective_to=None,
        )
        s.add(alias)
        await s.commit()
        await s.refresh(defn)
        return defn


@pytest.mark.asyncio
async def test_inventory_returns_empty_array_when_no_instruments(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/symbols/inventory",
        params={"start": "2025-01-01", "end": "2026-01-01"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.asyncio
async def test_inventory_returns_all_registered_instruments(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_active_alias(session_factory, raw_symbol="AAPL", asset_class="equity")
    await _seed_active_alias(
        session_factory,
        raw_symbol="ES",
        asset_class="futures",
        listing_venue="XCME",
        routing_venue="XCME",
    )

    response = await client.get(
        "/api/v1/symbols/inventory",
        params={"start": "2025-01-01", "end": "2026-01-01"},
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 2
    by_symbol = {r["symbol"]: r for r in rows}
    assert "AAPL" in by_symbol
    assert "ES" in by_symbol
    aapl = by_symbol["AAPL"]
    assert aapl["asset_class"] == "equity"
    assert aapl["provider"] == "databento"
    assert aapl["registered"] is True
    # _seed_active_alias defaults to provider=databento (no IB row), so live_qualified=False
    assert aapl["live_qualified"] is False
    # No Parquet seeded for these symbols → coverage status is "none" → backtest_only
    assert aapl["status"] == "backtest_only"


@pytest.mark.asyncio
async def test_inventory_filters_by_asset_class(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_active_alias(session_factory, raw_symbol="AAPL", asset_class="equity")
    await _seed_active_alias(
        session_factory,
        raw_symbol="ES",
        asset_class="futures",
        listing_venue="XCME",
        routing_venue="XCME",
    )

    response = await client.get(
        "/api/v1/symbols/inventory",
        params={
            "start": "2025-01-01",
            "end": "2026-01-01",
            "asset_class": "futures",
        },
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "ES"
    assert rows[0]["asset_class"] == "futures"


@pytest.mark.asyncio
async def test_inventory_without_window_returns_null_coverage(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_active_alias(session_factory, raw_symbol="AAPL", asset_class="equity")

    response = await client.get("/api/v1/symbols/inventory")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "AAPL"
    assert row["backtest_data_available"] is None
    assert row["coverage_status"] is None
    assert row["covered_range"] is None
    assert row["missing_ranges"] == []


@pytest.mark.asyncio
async def test_inventory_excludes_hidden_rows(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """B6a's hidden_from_inventory column filter."""
    from sqlalchemy import update

    await _seed_active_alias(session_factory, raw_symbol="AAPL", asset_class="equity")
    async with session_factory() as s:
        await s.execute(
            update(InstrumentDefinition)
            .where(InstrumentDefinition.raw_symbol == "AAPL")
            .values(hidden_from_inventory=True)
        )
        await s.commit()

    response = await client.get(
        "/api/v1/symbols/inventory",
        params={"start": "2025-01-01", "end": "2026-01-01"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == []  # AAPL hidden


@pytest.mark.asyncio
async def test_delete_hides_from_inventory(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DELETE marks the symbol hidden; subsequent /inventory excludes it."""
    await _seed_active_alias(session_factory, raw_symbol="AAPL", asset_class="equity")

    inv1 = await client.get(
        "/api/v1/symbols/inventory",
        params={"start": "2024-01-01", "end": "2025-01-01"},
    )
    assert any(r["symbol"] == "AAPL" for r in inv1.json())

    r2 = await client.delete("/api/v1/symbols/AAPL", params={"asset_class": "equity"})
    assert r2.status_code == 204, r2.text

    inv2 = await client.get(
        "/api/v1/symbols/inventory",
        params={"start": "2024-01-01", "end": "2025-01-01"},
    )
    assert not any(r["symbol"] == "AAPL" for r in inv2.json())


@pytest.mark.asyncio
async def test_delete_unknown_symbol_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.delete(
        "/api/v1/symbols/UNKNOWN",
        params={"asset_class": "equity"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
