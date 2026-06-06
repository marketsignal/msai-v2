"""Integration tests for GET /api/v1/symbols/readiness (T10).

Validates the three-state readiness contract (pin #3):

- Unregistered ``(symbol, asset_class)`` → HTTP 404 NOT_FOUND.
- Registered + window provided → ``backtest_data_available`` derived from
  Parquet coverage scan.
- Registered + no window → ``backtest_data_available=null`` +
  ``coverage_summary`` hint (operator UX affordance).
"""

from __future__ import annotations

import calendar as _calendar
from datetime import date
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from msai.api.symbol_onboarding import router as symbol_onboarding_router
from msai.core.auth import get_current_user
from msai.core.config import settings
from msai.core.database import get_db
from msai.models.instrument_alias import InstrumentAlias
from msai.models.instrument_definition import InstrumentDefinition
from msai.services.symbol_onboarding.partition_index_db import PartitionIndexGateway
from tests.conftest import make_partition_row_from_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

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


async def _seed_dual_provider(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    raw_symbol: str,
    asset_class: str = "equity",
    listing_venue: str = "XNAS",
    routing_venue: str = "XNAS",
) -> dict[str, InstrumentDefinition]:
    """Seed coexisting ``databento`` + ``interactive_brokers`` definition+alias
    rows for the SAME ``(raw_symbol, asset_class)``.

    Mirrors the cross-provider coexistence pattern in
    ``test_instrument_registry.py::test_find_by_raw_symbol_requires_provider``
    (schema uniqueness is ``(raw_symbol, provider, asset_class)``). This is the
    structurally-always-ambiguous state that the readiness/remove handlers must
    now disambiguate via the ``provider`` query param.

    Returns the two definitions keyed by provider so callers can assert on the
    surviving uid after a provider-scoped delete.
    """
    defns: dict[str, InstrumentDefinition] = {}
    for provider in ("databento", "interactive_brokers"):
        defns[provider] = await _seed_active_alias(
            session_factory,
            raw_symbol=raw_symbol,
            asset_class=asset_class,
            provider=provider,
            listing_venue=listing_venue,
            routing_venue=routing_venue,
        )
    return defns


@pytest.mark.asyncio
async def test_readiness_dual_provider_unpinned_returns_primary(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """THE BUG: unpinned readiness on a dual-provider symbol must answer 200
    with the preferred primary (databento) named — pre-fix this 422'd with an
    unsatisfiable 'pin provider explicitly'."""
    await _seed_dual_provider(session_factory, raw_symbol="SPY")

    resp = await client.get(
        "/api/v1/symbols/readiness",
        params={"symbol": "SPY", "asset_class": "equity"},
    )

    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["registered"] is True
    assert body["provider"] == "databento"
    # No window → coverage fields are null, but the symbol resolves and the
    # caller gets a usable readiness answer instead of the 422 dead-end.
    assert body["backtest_data_available"] is None
    assert body["coverage_status"] is None
    # Aggregate live-qualification: an IB alias exists somewhere across the
    # symbol's providers, so unpinned readiness reports True.
    assert body["live_qualified"] is True


@pytest.mark.asyncio
async def test_readiness_dual_provider_pinned_ib_returns_ib_view(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pinning provider=interactive_brokers scopes the metadata block to the
    IB row; coverage stays provider-invariant."""
    await _seed_dual_provider(session_factory, raw_symbol="SPY")

    resp = await client.get(
        "/api/v1/symbols/readiness",
        params={
            "symbol": "SPY",
            "asset_class": "equity",
            "provider": "interactive_brokers",
        },
    )

    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["registered"] is True
    assert body["provider"] == "interactive_brokers"
    # Scoped: the IB row itself carries an IB alias → qualifies.
    assert body["live_qualified"] is True
    assert body["backtest_data_available"] is None
    assert body["coverage_status"] is None


@pytest.mark.asyncio
async def test_readiness_dual_provider_pinned_databento_scopes_live_qualified(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pinning provider=databento scopes live_qualified to the databento row,
    which has no IB alias → False (contrast the unpinned aggregate True)."""
    await _seed_dual_provider(session_factory, raw_symbol="SPY")

    resp = await client.get(
        "/api/v1/symbols/readiness",
        params={
            "symbol": "SPY",
            "asset_class": "equity",
            "provider": "databento",
        },
    )

    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["registered"] is True
    assert body["provider"] == "databento"
    # SCOPED: the databento row alone has no IB alias.
    assert body["live_qualified"] is False


@pytest.mark.asyncio
async def test_readiness_pinned_provider_with_no_row_is_actionable_4xx(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pinning a provider that has no row for the symbol returns an actionable
    404 NOT_FOUND whose message names BOTH the symbol and the pinned provider so
    the caller can correct the request."""
    await _seed_active_alias(
        session_factory,
        raw_symbol="DBONLY",
        asset_class="equity",
        provider="databento",
    )

    resp = await client.get(
        "/api/v1/symbols/readiness",
        params={
            "symbol": "DBONLY",
            "asset_class": "equity",
            "provider": "interactive_brokers",
        },
    )

    assert resp.status_code == 404, resp.text
    body: dict[str, Any] = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "DBONLY" in body["error"]["message"]
    assert "interactive_brokers" in body["error"]["message"]


@pytest.mark.asyncio
async def test_remove_dual_provider_unpinned_is_satisfiable_422_not_500(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unpinned DELETE on a dual-provider symbol stays explicit (destructive ops
    require a provider) but is now a SATISFIABLE 422 — pre-fix it bubbled as an
    unhandled 500 because remove_symbol had no AmbiguousSymbolError handler."""
    await _seed_dual_provider(session_factory, raw_symbol="SPY")

    resp = await client.delete(
        "/api/v1/symbols/SPY",
        params={"asset_class": "equity"},
    )

    assert resp.status_code == 422, resp.text
    body: dict[str, Any] = resp.json()
    assert body["error"]["code"] == "AMBIGUOUS_INSTRUMENT"
    # The instruction is now satisfiable: it names the providers to pin.
    assert "provider" in body["error"]["message"].lower()
    # The 422 must name BOTH providers so the operator knows which to pin.
    assert "interactive_brokers" in body["error"]["message"]
    assert "databento" in body["error"]["message"]


@pytest.mark.asyncio
async def test_remove_dual_provider_pinned_deletes_only_that_provider(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pinned DELETE removes only that provider's row; the other provider's row
    survives and remains visible in the inventory."""
    await _seed_dual_provider(session_factory, raw_symbol="SPY")

    resp = await client.delete(
        "/api/v1/symbols/SPY",
        params={"asset_class": "equity", "provider": "interactive_brokers"},
    )
    assert resp.status_code == 204, resp.text

    inv = await client.get("/api/v1/symbols/inventory")
    assert inv.status_code == 200, inv.text
    rows = [r for r in inv.json() if r["symbol"] == "SPY"]
    providers = {r["provider"] for r in rows}
    # Only the IB row was hidden; the databento row remains.
    assert providers == {"databento"}


@pytest.mark.asyncio
async def test_readiness_404_for_unregistered_symbol(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get(
        "/api/v1/symbols/readiness",
        params={"symbol": "ZZZZ", "asset_class": "equity"},
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "ZZZZ" in body["error"]["message"]


@pytest.mark.asyncio
async def test_readiness_with_window_returns_scoped_availability(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write_partition: Any,
) -> None:
    # Arrange — registry row + Parquet months covering the requested window.
    # Scope-B: the coverage scan reads ``parquet_partition_index``, not the
    # filesystem directly, so we write real partitions AND seed the index.
    await _seed_active_alias(
        session_factory,
        raw_symbol="SPY",
        asset_class="equity",
        provider="databento",
    )
    for month in (1, 2, 3):
        last_day = _calendar.monthrange(2024, month)[1]
        days = list(range(1, last_day + 1))
        path = write_partition(
            tmp_path,
            asset_class="stocks",
            symbol="SPY",
            year=2024,
            month=month,
            days=days,
        )
        row = make_partition_row_from_path(
            path,
            asset_class="stocks",
            symbol="SPY",
            year=2024,
            month=month,
            days=days,
        )
        async with session_factory() as session:
            await PartitionIndexGateway(session=session).upsert(row)
    monkeypatch.setattr(settings, "data_root", str(tmp_path), raising=False)

    # Act
    resp = await client.get(
        "/api/v1/symbols/readiness",
        params={
            "symbol": "SPY",
            "asset_class": "equity",
            "start": "2024-01-01",
            "end": "2024-03-31",
        },
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["registered"] is True
    assert body["provider"] == "databento"
    assert body["backtest_data_available"] is True
    assert body["coverage_status"] == "full"
    # covered_range now reflects the trading-day min/max within the window
    # (Scope-B refactor): Jan 1 2024 is New Year's Day (closed); first
    # trading day is Jan 2. Mar 29 is Good Friday; last trading day in the
    # Jan–Mar window is Mar 28 (Mar 30/31 are weekend).
    assert body["covered_range"] == "2024-01-02 → 2024-03-28"
    assert body["missing_ranges"] == []
    assert body["live_qualified"] is False
    assert body["coverage_summary"] is None


@pytest.mark.asyncio
async def test_readiness_without_window_returns_null_available(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Arrange — registry row only (no Parquet seeding required for this branch).
    await _seed_active_alias(
        session_factory,
        raw_symbol="AAPL",
        asset_class="equity",
        provider="databento",
    )

    # Act — omit start + end
    resp = await client.get(
        "/api/v1/symbols/readiness",
        params={"symbol": "AAPL", "asset_class": "equity"},
    )

    # Assert
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["registered"] is True
    assert body["provider"] == "databento"
    assert body["backtest_data_available"] is None
    assert body["coverage_status"] is None
    assert body["covered_range"] is None
    assert body["missing_ranges"] == []
    assert body["live_qualified"] is False
    assert isinstance(body["coverage_summary"], str)
    assert "databento" in body["coverage_summary"]
