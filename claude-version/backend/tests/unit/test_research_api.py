"""Unit tests for the research API endpoints."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRATEGY_ID = uuid4()
_JOB_ID = uuid4()


def _make_strategy_row() -> MagicMock:
    """Return a mock Strategy row with all fields the API needs."""
    row = MagicMock()
    row.id = _STRATEGY_ID
    row.name = "test_ema_cross"
    row.file_path = "/app/strategies/ema_cross.py"
    return row


def _make_job_row(
    *,
    job_id: UUID | None = None,
    status: str = "pending",
    job_type: str = "parameter_sweep",
) -> MagicMock:
    """Return a mock ResearchJob row."""
    row = MagicMock()
    row.id = job_id or _JOB_ID
    row.strategy_id = _STRATEGY_ID
    row.job_type = job_type
    row.status = status
    row.progress = 0
    row.progress_message = None
    row.best_config = {"period": 20} if status == "completed" else None
    row.best_metrics = {"sharpe_ratio": 1.5} if status == "completed" else None
    row.error_message = None
    row.started_at = None
    row.completed_at = None
    row.created_at = datetime.now(UTC)
    row.config = {"strategy_path": "/app/strategies/ema_cross.py"}
    row.results = None
    row.queue_name = "msai:research"
    row.queue_job_id = "arq-123"
    return row


def _make_trial_row(trial_number: int = 0) -> MagicMock:
    """Return a mock ResearchTrial row."""
    row = MagicMock()
    row.id = uuid4()
    row.trial_number = trial_number
    row.config = {"period": 20}
    row.metrics = {"sharpe_ratio": 1.5}
    row.status = "completed"
    row.objective_value = 1.5
    row.backtest_id = None
    row.created_at = datetime.now(UTC)
    return row


def _make_candidate_row() -> MagicMock:
    """Return a mock GraduationCandidate row."""
    row = MagicMock()
    row.id = uuid4()
    row.stage = "discovery"
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


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/research/sweeps
# ---------------------------------------------------------------------------


class TestSubmitParameterSweep:
    """Tests for POST /api/v1/research/sweeps."""

    @patch("msai.api.research.get_redis_pool")
    @patch("msai.api.research.enqueue_research")
    @patch("pathlib.Path.exists", return_value=True)
    async def test_submit_sweep_creates_job_returns_201(
        self,
        _mock_exists: MagicMock,
        mock_enqueue: AsyncMock,
        mock_pool: AsyncMock,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /sweeps creates a ResearchJob and returns 201 with job_id."""
        # Arrange: strategy lookup succeeds
        strategy = _make_strategy_row()
        strategy_result = MagicMock()
        strategy_result.scalar_one_or_none.return_value = strategy
        mock_db.execute.return_value = strategy_result

        mock_pool.return_value = AsyncMock()
        mock_enqueue.return_value = "arq-job-123"

        # Make refresh populate the mock job with required fields
        async def _fake_refresh(obj: MagicMock) -> None:
            obj.id = _JOB_ID
            obj.strategy_id = _STRATEGY_ID
            obj.job_type = "parameter_sweep"
            obj.status = "pending"
            obj.progress = 0
            obj.progress_message = None
            obj.best_config = None
            obj.best_metrics = None
            obj.error_message = None
            obj.started_at = None
            obj.completed_at = None
            obj.created_at = datetime.now(UTC)

        mock_db.refresh.side_effect = _fake_refresh

        # Act
        response = await client_with_mock_db.post(
            "/api/v1/research/sweeps",
            json={
                "strategy_id": str(_STRATEGY_ID),
                "instruments": ["AAPL"],
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "parameter_grid": {"period": [10, 20, 30]},
            },
        )

        # Assert
        assert response.status_code == 201
        body = response.json()
        assert body["id"] == str(_JOB_ID)
        assert body["status"] == "pending"
        assert body["job_type"] == "parameter_sweep"

    async def test_submit_sweep_with_missing_strategy_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /sweeps with non-existent strategy_id returns 404."""
        # Arrange: strategy lookup returns None
        strategy_result = MagicMock()
        strategy_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = strategy_result

        response = await client_with_mock_db.post(
            "/api/v1/research/sweeps",
            json={
                "strategy_id": str(uuid4()),
                "instruments": ["AAPL"],
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
                "parameter_grid": {"period": [10, 20]},
            },
        )

        assert response.status_code == 404

    async def test_submit_sweep_without_grid_returns_422(
        self,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /sweeps without parameter_grid fails validation."""
        response = await client_with_mock_db.post(
            "/api/v1/research/sweeps",
            json={
                "strategy_id": str(uuid4()),
                "instruments": ["AAPL"],
                "start_date": "2024-01-01",
                "end_date": "2024-12-31",
            },
        )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/research/jobs
# ---------------------------------------------------------------------------


class TestListResearchJobs:
    """Tests for GET /api/v1/research/jobs."""

    async def test_list_jobs_returns_200_with_empty_list(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /jobs returns 200 with paginated empty results."""
        response = await client_with_mock_db.get("/api/v1/research/jobs")

        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 0
        assert body["items"] == []

    async def test_list_jobs_accepts_pagination_params(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /jobs accepts page and page_size query params."""
        response = await client_with_mock_db.get(
            "/api/v1/research/jobs?page=2&page_size=10"
        )

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/research/jobs/{job_id}
# ---------------------------------------------------------------------------


class TestGetResearchJob:
    """Tests for GET /api/v1/research/jobs/{job_id}."""

    async def test_get_job_not_found_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /jobs/{id} with non-existent ID returns 404."""
        mock_db.get.return_value = None

        response = await client_with_mock_db.get(
            f"/api/v1/research/jobs/{uuid4()}"
        )

        assert response.status_code == 404

    async def test_get_job_returns_detail_with_trials(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /jobs/{id} returns job detail including trials."""
        # Arrange: job exists, trials exist
        job = _make_job_row(status="completed")
        mock_db.get.return_value = job

        trial = _make_trial_row(trial_number=0)
        trials_result = MagicMock()
        trials_scalars = MagicMock()
        trials_scalars.all.return_value = [trial]
        trials_result.scalars.return_value = trials_scalars
        mock_db.execute.return_value = trials_result

        response = await client_with_mock_db.get(
            f"/api/v1/research/jobs/{_JOB_ID}"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(_JOB_ID)
        assert "trials" in body
        assert len(body["trials"]) == 1
        assert body["trials"][0]["trial_number"] == 0


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/research/jobs/{job_id}/cancel
# ---------------------------------------------------------------------------


class TestCancelResearchJob:
    """Tests for POST /api/v1/research/jobs/{job_id}/cancel."""

    async def test_cancel_not_found_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /cancel with non-existent job returns 404."""
        mock_db.get.return_value = None

        response = await client_with_mock_db.post(
            f"/api/v1/research/jobs/{uuid4()}/cancel"
        )

        assert response.status_code == 404

    async def test_cancel_completed_job_returns_unchanged(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /cancel on completed job returns it unchanged."""
        job = _make_job_row(status="completed")
        mock_db.get.return_value = job

        response = await client_with_mock_db.post(
            f"/api/v1/research/jobs/{_JOB_ID}/cancel"
        )

        assert response.status_code == 200
        assert response.json()["status"] == "completed"

    async def test_cancel_pending_job_marks_cancelled(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /cancel on pending job sets status to cancelled."""
        job = _make_job_row(status="pending")
        mock_db.get.return_value = job

        response = await client_with_mock_db.post(
            f"/api/v1/research/jobs/{_JOB_ID}/cancel"
        )

        assert response.status_code == 200
        assert job.status == "cancelled"


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/research/promotions
# ---------------------------------------------------------------------------


class TestPromoteResearchResult:
    """Tests for POST /api/v1/research/promotions."""

    async def test_promote_not_found_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /promotions with non-existent job returns 404."""
        mock_db.get.return_value = None

        response = await client_with_mock_db.post(
            "/api/v1/research/promotions",
            json={"research_job_id": str(uuid4())},
        )

        assert response.status_code == 404

    async def test_promote_non_completed_returns_409(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /promotions on a running job returns 409."""
        job = _make_job_row(status="running")
        mock_db.get.return_value = job

        response = await client_with_mock_db.post(
            "/api/v1/research/promotions",
            json={"research_job_id": str(_JOB_ID)},
        )

        assert response.status_code == 409

    async def test_promote_completed_job_creates_candidate(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /promotions on a completed job creates a GraduationCandidate."""
        job = _make_job_row(status="completed")
        mock_db.get.return_value = job

        # refresh populates the candidate fields
        candidate_id = uuid4()

        async def _fake_refresh(obj: MagicMock) -> None:
            obj.id = candidate_id
            obj.stage = "discovery"

        mock_db.refresh.side_effect = _fake_refresh

        response = await client_with_mock_db.post(
            "/api/v1/research/promotions",
            json={
                "research_job_id": str(_JOB_ID),
                "notes": "Looks promising",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["candidate_id"] == str(candidate_id)
        assert body["stage"] == "discovery"
