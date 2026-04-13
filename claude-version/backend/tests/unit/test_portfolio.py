"""Unit tests for the portfolio management service and API."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app
from msai.schemas.portfolio import PortfolioCreate, PortfolioRunCreate
from msai.services.portfolio_service import PortfolioService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PORTFOLIO_ID = uuid4()
_CANDIDATE_ID = uuid4()
_RUN_ID = uuid4()


def _make_portfolio_row(
    *,
    portfolio_id: UUID | None = None,
    name: str = "Test Portfolio",
) -> MagicMock:
    """Return a mock Portfolio row."""
    row = MagicMock()
    row.id = portfolio_id or _PORTFOLIO_ID
    row.name = name
    row.description = "A test portfolio"
    row.objective = "max_sharpe"
    row.base_capital = 100000.0
    row.requested_leverage = 1.0
    row.benchmark_symbol = "SPY"
    row.account_id = None
    row.created_by = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def _make_allocation_row(
    *,
    portfolio_id: UUID | None = None,
    candidate_id: UUID | None = None,
    weight: float = 0.5,
) -> MagicMock:
    """Return a mock PortfolioAllocation row."""
    row = MagicMock()
    row.id = uuid4()
    row.portfolio_id = portfolio_id or _PORTFOLIO_ID
    row.candidate_id = candidate_id or _CANDIDATE_ID
    row.weight = weight
    row.created_at = datetime.now(UTC)
    return row


def _make_run_row(
    *,
    run_id: UUID | None = None,
    portfolio_id: UUID | None = None,
    status: str = "pending",
) -> MagicMock:
    """Return a mock PortfolioRun row."""
    row = MagicMock()
    row.id = run_id or _RUN_ID
    row.portfolio_id = portfolio_id or _PORTFOLIO_ID
    row.status = status
    row.metrics = None
    row.report_path = None
    row.start_date = date(2025, 1, 1)
    row.end_date = date(2025, 12, 31)
    row.created_by = None
    row.created_at = datetime.now(UTC)
    row.completed_at = None
    return row


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> AsyncMock:
    """Create a mock AsyncSession."""
    session = AsyncMock(spec=AsyncSession)

    # Default: execute returns empty results
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one.return_value = 0
    mock_result.scalar_one_or_none.return_value = None

    session.execute.return_value = mock_result
    session.get.return_value = None
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.rollback = AsyncMock()
    # begin_nested context manager for resolve_user_id
    nested_cm = AsyncMock()
    nested_cm.__aenter__ = AsyncMock()
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested.return_value = nested_cm
    return session


@pytest.fixture
def client_with_mock_db(mock_db: AsyncMock) -> httpx.AsyncClient:
    """Async test client with DB dependency overridden."""

    async def _override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    app.dependency_overrides[get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    yield client  # type: ignore[misc]
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def service() -> PortfolioService:
    """Return a PortfolioService instance."""
    return PortfolioService()


# ---------------------------------------------------------------------------
# Service tests: create
# ---------------------------------------------------------------------------


class TestCreatePortfolio:
    """Tests for PortfolioService.create."""

    async def test_create_portfolio_returns_portfolio(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """create returns a portfolio and creates allocation rows."""

        # Arrange: session.get returns a mock candidate for validation
        mock_db.get.return_value = MagicMock()

        # Arrange: flush assigns an id
        async def _flush() -> None:
            for call_args in mock_db.add.call_args_list:
                obj = call_args[0][0]
                if hasattr(obj, "name") and not hasattr(obj, "weight"):
                    obj.id = _PORTFOLIO_ID

        mock_db.flush.side_effect = _flush

        data = PortfolioCreate(
            name="Test Portfolio",
            objective="max_sharpe",
            base_capital=100000.0,
            allocations=[
                {"candidate_id": str(_CANDIDATE_ID), "weight": 0.6},
                {"candidate_id": str(uuid4()), "weight": 0.4},
            ],
        )

        # Act
        result = await service.create(mock_db, data)

        # Assert: 1 portfolio + 2 allocations = 3 add() calls
        assert result.name == "Test Portfolio"
        assert result.objective == "max_sharpe"
        assert mock_db.add.call_count == 3

    async def test_create_portfolio_with_user_id(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """create sets created_by when user_id is provided."""
        mock_db.get.return_value = MagicMock()

        async def _flush() -> None:
            for call_args in mock_db.add.call_args_list:
                obj = call_args[0][0]
                if hasattr(obj, "name") and not hasattr(obj, "weight"):
                    obj.id = _PORTFOLIO_ID

        mock_db.flush.side_effect = _flush
        user_id = uuid4()

        data = PortfolioCreate(
            name="My Portfolio",
            objective="equal_weight",
            base_capital=50000.0,
            allocations=[
                {"candidate_id": str(_CANDIDATE_ID), "weight": 1.0},
            ],
        )

        result = await service.create(mock_db, data, user_id=user_id)

        assert result.created_by == user_id


# ---------------------------------------------------------------------------
# Service tests: list / get
# ---------------------------------------------------------------------------


class TestListAndGet:
    """Tests for PortfolioService.list and get."""

    async def test_list_empty(self, service: PortfolioService, mock_db: AsyncMock) -> None:
        """list returns empty list when no portfolios exist."""
        result = await service.list(mock_db)
        assert result == []

    async def test_list_returns_portfolios(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """list returns portfolios."""
        p1 = _make_portfolio_row(name="Portfolio A")
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [p1]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await service.list(mock_db)
        assert len(result) == 1
        assert result[0].name == "Portfolio A"

    async def test_get_not_found_raises(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """get raises ValueError for non-existent portfolio."""
        mock_db.get.return_value = None
        with pytest.raises(ValueError, match="not found"):
            await service.get(mock_db, uuid4())

    async def test_get_returns_portfolio(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """get returns portfolio when it exists."""
        portfolio = _make_portfolio_row()
        mock_db.get.return_value = portfolio

        result = await service.get(mock_db, _PORTFOLIO_ID)
        assert result.id == _PORTFOLIO_ID


# ---------------------------------------------------------------------------
# Service tests: get_allocations
# ---------------------------------------------------------------------------


class TestGetAllocations:
    """Tests for PortfolioService.get_allocations."""

    async def test_get_allocations_returns_list(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """get_allocations returns allocations for a portfolio."""
        a1 = _make_allocation_row(weight=0.6)
        a2 = _make_allocation_row(weight=0.4)

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [a1, a2]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await service.get_allocations(mock_db, _PORTFOLIO_ID)
        assert len(result) == 2

    async def test_get_allocations_empty(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """get_allocations returns empty list when no allocations exist."""
        result = await service.get_allocations(mock_db, _PORTFOLIO_ID)
        assert result == []


# ---------------------------------------------------------------------------
# Service tests: runs
# ---------------------------------------------------------------------------


class TestPortfolioRuns:
    """Tests for PortfolioService run-related methods."""

    async def test_create_run_validates_portfolio_exists(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """create_run raises ValueError if portfolio not found."""
        mock_db.get.return_value = None
        data = PortfolioRunCreate(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )

        with pytest.raises(ValueError, match="not found"):
            await service.create_run(mock_db, uuid4(), data)

    async def test_create_run_returns_pending_run(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """create_run creates a run in 'pending' status."""
        portfolio = _make_portfolio_row()
        mock_db.get.return_value = portfolio

        async def _flush() -> None:
            for call_args in mock_db.add.call_args_list:
                obj = call_args[0][0]
                if hasattr(obj, "portfolio_id") and hasattr(obj, "status"):
                    obj.id = _RUN_ID

        mock_db.flush.side_effect = _flush

        data = PortfolioRunCreate(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )

        result = await service.create_run(mock_db, _PORTFOLIO_ID, data)
        assert result.status == "pending"
        assert result.portfolio_id == _PORTFOLIO_ID

    async def test_list_runs_empty(self, service: PortfolioService, mock_db: AsyncMock) -> None:
        """list_runs returns empty list when no runs exist."""
        result = await service.list_runs(mock_db)
        assert result == []

    async def test_list_runs_with_portfolio_filter(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """list_runs filters by portfolio_id when provided."""
        r1 = _make_run_row()
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [r1]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await service.list_runs(mock_db, portfolio_id=_PORTFOLIO_ID)
        assert len(result) == 1

    async def test_get_run_not_found_raises(
        self, service: PortfolioService, mock_db: AsyncMock
    ) -> None:
        """get_run raises ValueError for non-existent run."""
        mock_db.get.return_value = None
        with pytest.raises(ValueError, match="not found"):
            await service.get_run(mock_db, uuid4())

    async def test_get_run_returns_run(self, service: PortfolioService, mock_db: AsyncMock) -> None:
        """get_run returns run when it exists."""
        run = _make_run_row()
        mock_db.get.return_value = run

        result = await service.get_run(mock_db, _RUN_ID)
        assert result.id == _RUN_ID


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class TestPortfolioAPI:
    """Tests for the portfolio API endpoints."""

    async def test_list_portfolios_returns_200(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /api/v1/portfolios returns 200 with paginated results."""
        response = await client_with_mock_db.get("/api/v1/portfolios")

        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 0

    async def test_get_portfolio_not_found_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /api/v1/portfolios/{id} with non-existent ID returns 404."""
        response = await client_with_mock_db.get(f"/api/v1/portfolios/{uuid4()}")

        assert response.status_code == 404

    async def test_get_portfolio_returns_200(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /api/v1/portfolios/{id} with existing portfolio returns 200."""
        portfolio = _make_portfolio_row()
        mock_db.get.return_value = portfolio

        response = await client_with_mock_db.get(f"/api/v1/portfolios/{_PORTFOLIO_ID}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(_PORTFOLIO_ID)
        assert body["name"] == "Test Portfolio"

    async def test_list_runs_returns_200(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /api/v1/portfolios/runs returns 200 with paginated results."""
        response = await client_with_mock_db.get("/api/v1/portfolios/runs")

        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body

    async def test_get_run_not_found_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /api/v1/portfolios/runs/{id} with non-existent ID returns 404."""
        response = await client_with_mock_db.get(f"/api/v1/portfolios/runs/{uuid4()}")

        assert response.status_code == 404

    async def test_get_run_returns_200(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /api/v1/portfolios/runs/{id} with existing run returns 200."""
        run = _make_run_row()
        mock_db.get.return_value = run

        response = await client_with_mock_db.get(f"/api/v1/portfolios/runs/{_RUN_ID}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(_RUN_ID)
        assert body["status"] == "pending"

    async def test_get_run_report_no_report_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /api/v1/portfolios/runs/{id}/report with no report returns 404."""
        run = _make_run_row()
        run.report_path = None
        mock_db.get.return_value = run

        response = await client_with_mock_db.get(f"/api/v1/portfolios/runs/{_RUN_ID}/report")

        assert response.status_code == 404

    async def test_portfolio_router_registered(self) -> None:
        """Verify the portfolio router is registered on the app."""
        routes = [route.path for route in app.routes]
        assert "/api/v1/portfolios" in routes or any(
            r.startswith("/api/v1/portfolios")
            for r in routes  # type: ignore[union-attr]
        )
