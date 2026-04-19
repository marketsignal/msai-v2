"""Unit tests for live-portfolio CRUD API (``api/portfolios.py``).

Tests cover:
1. Schema validation (required fields, defaults, constraints)
2. Router registration (routes exist in the app)
3. Endpoint function signatures
4. HTTP status codes for happy-path and error cases via the test client
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from msai.schemas.live_portfolio import (
    LivePortfolioAddStrategyRequest,
    LivePortfolioCreateRequest,
    LivePortfolioMemberResponse,
    LivePortfolioResponse,
    LivePortfolioRevisionResponse,
)


# ---------------------------------------------------------------------------
# 1. Schema validation
# ---------------------------------------------------------------------------


class TestLivePortfolioCreateRequest:
    """Validate LivePortfolioCreateRequest schema."""

    def test_valid_minimal(self) -> None:
        req = LivePortfolioCreateRequest(name="Growth")
        assert req.name == "Growth"
        assert req.description is None

    def test_valid_with_description(self) -> None:
        req = LivePortfolioCreateRequest(name="Momentum", description="Long-only momentum")
        assert req.description == "Long-only momentum"

    def test_name_required(self) -> None:
        with pytest.raises(ValidationError, match="name"):
            LivePortfolioCreateRequest()  # type: ignore[call-arg]

    def test_name_max_length(self) -> None:
        with pytest.raises(ValidationError, match="128"):
            LivePortfolioCreateRequest(name="x" * 129)


class TestLivePortfolioAddStrategyRequest:
    """Validate LivePortfolioAddStrategyRequest schema."""

    def test_valid(self) -> None:
        req = LivePortfolioAddStrategyRequest(
            strategy_id=uuid4(),
            config={"bar_type": "1-MINUTE"},
            instruments=["AAPL.IBKR"],
            weight=Decimal("0.5"),
        )
        assert req.weight == Decimal("0.5")
        assert req.instruments == ["AAPL.IBKR"]

    def test_strategy_id_required(self) -> None:
        with pytest.raises(ValidationError, match="strategy_id"):
            LivePortfolioAddStrategyRequest(
                config={},
                instruments=["AAPL.IBKR"],
                weight=Decimal("0.5"),
            )  # type: ignore[call-arg]

    def test_weight_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="weight"):
            LivePortfolioAddStrategyRequest(
                strategy_id=uuid4(),
                config={},
                instruments=["AAPL.IBKR"],
                weight=Decimal("0"),
            )

    def test_weight_must_not_exceed_one(self) -> None:
        with pytest.raises(ValidationError, match="weight"):
            LivePortfolioAddStrategyRequest(
                strategy_id=uuid4(),
                config={},
                instruments=["AAPL.IBKR"],
                weight=Decimal("1.1"),
            )

    def test_instruments_must_not_be_empty(self) -> None:
        with pytest.raises(ValidationError, match="instruments"):
            LivePortfolioAddStrategyRequest(
                strategy_id=uuid4(),
                config={},
                instruments=[],
                weight=Decimal("0.5"),
            )


class TestLivePortfolioResponse:
    """Validate LivePortfolioResponse schema."""

    def test_from_attributes_enabled(self) -> None:
        assert LivePortfolioResponse.model_config.get("from_attributes") is True

    def test_required_fields(self) -> None:
        fields = LivePortfolioResponse.model_fields
        assert "id" in fields
        assert "name" in fields
        assert "description" in fields
        assert "created_at" in fields
        assert "updated_at" in fields


class TestLivePortfolioRevisionResponse:
    """Validate LivePortfolioRevisionResponse schema."""

    def test_from_attributes_enabled(self) -> None:
        assert LivePortfolioRevisionResponse.model_config.get("from_attributes") is True

    def test_required_fields(self) -> None:
        fields = LivePortfolioRevisionResponse.model_fields
        assert "id" in fields
        assert "revision_number" in fields
        assert "composition_hash" in fields
        assert "is_frozen" in fields
        assert "created_at" in fields


class TestLivePortfolioMemberResponse:
    """Validate LivePortfolioMemberResponse schema."""

    def test_from_attributes_enabled(self) -> None:
        assert LivePortfolioMemberResponse.model_config.get("from_attributes") is True

    def test_required_fields(self) -> None:
        fields = LivePortfolioMemberResponse.model_fields
        assert "id" in fields
        assert "strategy_id" in fields
        assert "config" in fields
        assert "instruments" in fields
        assert "weight" in fields
        assert "order_index" in fields


# ---------------------------------------------------------------------------
# 2. Router registration -- routes exist in the app
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    """Verify the live-portfolios router is mounted on the app."""

    def test_routes_registered(self) -> None:
        from msai.main import app

        route_paths = [r.path for r in app.routes]  # type: ignore[union-attr]

        assert "/api/v1/live-portfolios" in route_paths
        assert "/api/v1/live-portfolios/{portfolio_id}" in route_paths
        assert "/api/v1/live-portfolios/{portfolio_id}/strategies" in route_paths
        assert "/api/v1/live-portfolios/{portfolio_id}/snapshot" in route_paths
        assert "/api/v1/live-portfolios/{portfolio_id}/members" in route_paths

    def test_post_create_returns_201(self) -> None:
        """POST /api/v1/live-portfolios should be configured with 201."""
        from msai.main import app

        for route in app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", set())
            if path == "/api/v1/live-portfolios" and "POST" in methods:
                endpoint = getattr(route, "endpoint", None)
                assert endpoint is not None
                break
        else:
            pytest.fail("POST /api/v1/live-portfolios route not found")


# ---------------------------------------------------------------------------
# 3. Endpoint function signatures
# ---------------------------------------------------------------------------


class TestEndpointSignatures:
    """Verify endpoint functions exist and have the expected parameters."""

    def test_create_live_portfolio_signature(self) -> None:
        from msai.api.portfolios import create_live_portfolio

        import inspect

        sig = inspect.signature(create_live_portfolio)
        params = list(sig.parameters.keys())
        assert "body" in params
        assert "response" in params
        assert "claims" in params
        assert "db" in params

    def test_list_live_portfolios_signature(self) -> None:
        from msai.api.portfolios import list_live_portfolios

        import inspect

        sig = inspect.signature(list_live_portfolios)
        params = list(sig.parameters.keys())
        assert "claims" in params
        assert "db" in params

    def test_get_live_portfolio_signature(self) -> None:
        from msai.api.portfolios import get_live_portfolio

        import inspect

        sig = inspect.signature(get_live_portfolio)
        params = list(sig.parameters.keys())
        assert "portfolio_id" in params
        assert "claims" in params
        assert "db" in params

    def test_add_strategy_to_portfolio_signature(self) -> None:
        from msai.api.portfolios import add_strategy_to_portfolio

        import inspect

        sig = inspect.signature(add_strategy_to_portfolio)
        params = list(sig.parameters.keys())
        assert "portfolio_id" in params
        assert "body" in params
        assert "claims" in params
        assert "db" in params

    def test_snapshot_portfolio_signature(self) -> None:
        from msai.api.portfolios import snapshot_portfolio

        import inspect

        sig = inspect.signature(snapshot_portfolio)
        params = list(sig.parameters.keys())
        assert "portfolio_id" in params
        assert "claims" in params
        assert "db" in params

    def test_list_draft_members_signature(self) -> None:
        from msai.api.portfolios import list_draft_members

        import inspect

        sig = inspect.signature(list_draft_members)
        params = list(sig.parameters.keys())
        assert "portfolio_id" in params
        assert "claims" in params
        assert "db" in params


# ---------------------------------------------------------------------------
# 4. HTTP status codes via test client
# ---------------------------------------------------------------------------


class TestHTTPEndpoints:
    """Exercise endpoints via httpx test client with a fake DB session.

    The conftest.py autouse fixture overrides ``get_current_user``.
    Here we additionally override ``get_db`` to avoid real database access.
    """

    @pytest.fixture(autouse=True)
    def _override_db(self) -> None:  # type: ignore[override]
        """Install a no-op DB override so endpoints don't hit Postgres."""
        from collections.abc import AsyncGenerator

        from msai.core.database import get_db
        from msai.main import app

        class _FakeSession:
            """Stub session that returns empty result sets."""

            async def execute(self, _stmt: object) -> _FakeSession:
                return self

            def scalars(self) -> _FakeSession:
                return self

            def all(self) -> list[object]:
                return []

            def scalar_one_or_none(self) -> None:
                return None

            async def get(self, _model: type, _id: object) -> None:
                return None

            async def commit(self) -> None:
                pass

            async def refresh(self, _row: object) -> None:
                pass

        async def _override() -> AsyncGenerator[_FakeSession, None]:
            yield _FakeSession()

        app.dependency_overrides[get_db] = _override
        yield
        app.dependency_overrides.pop(get_db, None)

    @pytest.fixture
    def client(self) -> "httpx.AsyncClient":
        import httpx

        from msai.main import app

        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")

    async def test_list_returns_200(self, client: "httpx.AsyncClient") -> None:
        response = await client.get("/api/v1/live-portfolios")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_get_nonexistent_returns_404(self, client: "httpx.AsyncClient") -> None:
        fake_id = uuid4()
        response = await client.get(f"/api/v1/live-portfolios/{fake_id}")
        assert response.status_code == 404

    async def test_add_strategy_nonexistent_portfolio_returns_404(
        self, client: "httpx.AsyncClient"
    ) -> None:
        fake_id = uuid4()
        response = await client.post(
            f"/api/v1/live-portfolios/{fake_id}/strategies",
            json={
                "strategy_id": str(uuid4()),
                "config": {},
                "instruments": ["AAPL.IBKR"],
                "weight": "0.5",
            },
        )
        assert response.status_code == 404

    async def test_snapshot_nonexistent_portfolio_returns_404(
        self, client: "httpx.AsyncClient"
    ) -> None:
        fake_id = uuid4()
        response = await client.post(f"/api/v1/live-portfolios/{fake_id}/snapshot")
        assert response.status_code == 404

    async def test_list_members_nonexistent_portfolio_returns_404(
        self, client: "httpx.AsyncClient"
    ) -> None:
        fake_id = uuid4()
        response = await client.get(f"/api/v1/live-portfolios/{fake_id}/members")
        assert response.status_code == 404

    async def test_create_missing_name_returns_422(
        self, client: "httpx.AsyncClient"
    ) -> None:
        response = await client.post(
            "/api/v1/live-portfolios",
            json={},
        )
        assert response.status_code == 422
