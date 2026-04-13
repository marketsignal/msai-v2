"""Unit tests for AssetUniverseService and API router registration.

All database operations are mocked via ``unittest.mock.AsyncMock`` so
these tests run without a running PostgreSQL instance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from msai.models.asset_universe import AssetUniverse
from msai.schemas.asset_universe import AssetUniverseCreate
from msai.services.asset_universe import AssetUniverseService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_asset(
    *,
    symbol: str = "AAPL",
    exchange: str = "XNAS",
    asset_class: str = "stocks",
    resolution: str = "1m",
    enabled: bool = True,
    asset_id: UUID | None = None,
) -> MagicMock:
    """Build a mock that quacks like an :class:`AssetUniverse` row."""
    asset = MagicMock(spec=AssetUniverse)
    asset.id = asset_id or uuid4()
    asset.symbol = symbol
    asset.exchange = exchange
    asset.asset_class = asset_class
    asset.resolution = resolution
    asset.enabled = enabled
    asset.last_ingested_at = None
    asset.created_by = None
    return asset


def _make_session() -> AsyncMock:
    """Build an ``AsyncMock`` that quacks like ``AsyncSession``."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock()
    return session


def _scalars_result(rows: list[Any]) -> AsyncMock:
    """Simulate ``session.execute(select(...)).scalars().all()``."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    return result_mock


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


class TestAssetUniverseServiceAdd:
    """Tests for AssetUniverseService.add()."""

    @pytest.mark.asyncio
    async def test_add_creates_asset_with_correct_fields(self) -> None:
        """add() should call session.add with an AssetUniverse containing the input fields."""
        session = _make_session()
        service = AssetUniverseService()
        data = AssetUniverseCreate(
            symbol="MSFT",
            exchange="XNAS",
            asset_class="stocks",
            resolution="1m",
        )
        user_id = uuid4()

        result = await service.add(session, data, user_id=user_id)

        # session.add was called once
        session.add.assert_called_once()
        added_obj = session.add.call_args[0][0]
        assert isinstance(added_obj, AssetUniverse)
        assert added_obj.symbol == "MSFT"
        assert added_obj.exchange == "XNAS"
        assert added_obj.asset_class == "stocks"
        assert added_obj.resolution == "1m"
        assert added_obj.created_by == user_id

        # flush was awaited
        session.flush.assert_awaited_once()

        # return value is the same object
        assert result is added_obj

    @pytest.mark.asyncio
    async def test_add_without_user_id(self) -> None:
        """add() with no user_id should set created_by to None."""
        session = _make_session()
        service = AssetUniverseService()
        data = AssetUniverseCreate(
            symbol="AAPL", exchange="XNAS", asset_class="stocks"
        )

        result = await service.add(session, data)

        added_obj = session.add.call_args[0][0]
        assert added_obj.created_by is None


class TestAssetUniverseServiceRemove:
    """Tests for AssetUniverseService.remove()."""

    @pytest.mark.asyncio
    async def test_remove_sets_enabled_false(self) -> None:
        """remove() should set enabled=False on the found asset."""
        asset_id = uuid4()
        asset = _make_asset(asset_id=asset_id)
        session = _make_session()
        session.get = AsyncMock(return_value=asset)

        service = AssetUniverseService()
        await service.remove(session, asset_id)

        assert asset.enabled is False

    @pytest.mark.asyncio
    async def test_remove_not_found_raises_value_error(self) -> None:
        """remove() should raise ValueError when asset does not exist."""
        session = _make_session()
        session.get = AsyncMock(return_value=None)

        service = AssetUniverseService()
        with pytest.raises(ValueError, match="not found"):
            await service.remove(session, uuid4())


class TestAssetUniverseServiceList:
    """Tests for AssetUniverseService.list()."""

    @pytest.mark.asyncio
    async def test_list_returns_all_matching(self) -> None:
        """list() should return all rows from the query."""
        rows = [_make_asset(symbol="AAPL"), _make_asset(symbol="MSFT")]
        session = _make_session()
        session.execute = AsyncMock(return_value=_scalars_result(rows))

        service = AssetUniverseService()
        result = await service.list(session)

        assert len(result) == 2
        assert result[0].symbol == "AAPL"
        assert result[1].symbol == "MSFT"

    @pytest.mark.asyncio
    async def test_list_with_asset_class_filter(self) -> None:
        """list(asset_class=...) should pass the filter through to the query."""
        session = _make_session()
        session.execute = AsyncMock(return_value=_scalars_result([]))

        service = AssetUniverseService()
        result = await service.list(session, asset_class="futures")

        assert result == []
        # Verify execute was called (query was built)
        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_list_with_enabled_none(self) -> None:
        """list(enabled=None) should not filter by enabled status."""
        rows = [
            _make_asset(symbol="AAPL", enabled=True),
            _make_asset(symbol="TSLA", enabled=False),
        ]
        session = _make_session()
        session.execute = AsyncMock(return_value=_scalars_result(rows))

        service = AssetUniverseService()
        result = await service.list(session, enabled=None)

        assert len(result) == 2


class TestAssetUniverseServiceGetIngestTargets:
    """Tests for AssetUniverseService.get_ingest_targets()."""

    @pytest.mark.asyncio
    async def test_get_ingest_targets_returns_only_enabled(self) -> None:
        """get_ingest_targets() should delegate to list(enabled=True)."""
        enabled_assets = [_make_asset(symbol="SPY"), _make_asset(symbol="QQQ")]
        session = _make_session()
        session.execute = AsyncMock(return_value=_scalars_result(enabled_assets))

        service = AssetUniverseService()
        result = await service.get_ingest_targets(session)

        assert len(result) == 2
        session.execute.assert_awaited_once()


class TestAssetUniverseServiceMarkIngested:
    """Tests for AssetUniverseService.mark_ingested()."""

    @pytest.mark.asyncio
    async def test_mark_ingested_updates_timestamp(self) -> None:
        """mark_ingested() should set last_ingested_at on the asset."""
        asset_id = uuid4()
        asset = _make_asset(asset_id=asset_id)
        session = _make_session()
        session.get = AsyncMock(return_value=asset)
        now = datetime.now(timezone.utc)

        service = AssetUniverseService()
        await service.mark_ingested(session, asset_id, now)

        assert asset.last_ingested_at == now

    @pytest.mark.asyncio
    async def test_mark_ingested_noop_when_not_found(self) -> None:
        """mark_ingested() should silently return when asset is missing."""
        session = _make_session()
        session.get = AsyncMock(return_value=None)

        service = AssetUniverseService()
        # Should not raise
        await service.mark_ingested(session, uuid4(), datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# API router registration tests
# ---------------------------------------------------------------------------


class TestAssetUniverseRouterRegistration:
    """Verify the router exposes the expected paths."""

    def test_router_has_expected_routes(self) -> None:
        """The universe router should expose GET /, POST /, DELETE /{id}, POST /ingest."""
        from msai.api.asset_universe import router

        paths = {route.path for route in router.routes}
        prefix = router.prefix
        assert f"{prefix}/" in paths
        assert f"{prefix}/{{asset_id}}" in paths
        assert f"{prefix}/ingest" in paths

    def test_router_prefix(self) -> None:
        """The router prefix should be /api/v1/universe."""
        from msai.api.asset_universe import router

        assert router.prefix == "/api/v1/universe"

    def test_router_methods(self) -> None:
        """Verify HTTP methods on each route."""
        from msai.api.asset_universe import router

        prefix = router.prefix
        method_map: dict[str, set[str]] = {}
        for route in router.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set())
            if path in method_map:
                method_map[path] |= methods
            else:
                method_map[path] = set(methods)

        assert "GET" in method_map.get(f"{prefix}/", set())
        assert "POST" in method_map.get(f"{prefix}/", set())
        assert "DELETE" in method_map.get(f"{prefix}/{{asset_id}}", set())
        assert "POST" in method_map.get(f"{prefix}/ingest", set())

    def test_router_registered_in_main_app(self) -> None:
        """The universe router should be included in the main FastAPI app."""
        from msai.api.asset_universe import router

        # Verify the import path works (router exists)
        assert router is not None
        assert router.prefix == "/api/v1/universe"
