"""Unit tests for the graduation pipeline service and API."""

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
from msai.services.graduation import GraduationService, GraduationStageError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRATEGY_ID = uuid4()
_CANDIDATE_ID = uuid4()


def _make_candidate_row(
    *,
    candidate_id: UUID | None = None,
    stage: str = "discovery",
) -> MagicMock:
    """Return a mock GraduationCandidate row."""
    row = MagicMock()
    row.id = candidate_id or _CANDIDATE_ID
    row.strategy_id = _STRATEGY_ID
    row.research_job_id = None
    row.stage = stage
    row.config = {"period": 20}
    row.metrics = {"sharpe_ratio": 1.5}
    row.deployment_id = None
    row.notes = None
    row.promoted_by = None
    row.promoted_at = None
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def _make_transition_row(
    *,
    from_stage: str = "",
    to_stage: str = "discovery",
    reason: str | None = "Candidate created",
    seq: int = 1,
) -> MagicMock:
    """Return a mock GraduationStageTransition row."""
    row = MagicMock()
    row.id = seq
    row.candidate_id = _CANDIDATE_ID
    row.from_stage = from_stage
    row.to_stage = to_stage
    row.reason = reason
    row.transitioned_by = None
    row.created_at = datetime(2026, 1, 1, 0, 0, seq, tzinfo=UTC)
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

    # Default: session.get returns a MagicMock (satisfies FK validation and
    # candidate lookups). Tests that need 404 behavior override this per-test.
    session.get.return_value = MagicMock()
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


@pytest.fixture
def service() -> GraduationService:
    """Return a GraduationService instance."""
    return GraduationService()


# ---------------------------------------------------------------------------
# Service tests: create_candidate
# ---------------------------------------------------------------------------


class TestCreateCandidate:
    """Tests for GraduationService.create_candidate."""

    async def test_create_candidate_returns_discovery_stage(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """create_candidate returns a candidate in 'discovery' stage."""

        # Arrange: flush assigns an id via side_effect
        async def _flush() -> None:
            # Simulate SQLAlchemy flush assigning an id
            for call_args in mock_db.add.call_args_list:
                obj = call_args[0][0]
                if hasattr(obj, "strategy_id") and not hasattr(obj, "from_stage"):
                    obj.id = _CANDIDATE_ID

        mock_db.flush.side_effect = _flush

        # Act
        result = await service.create_candidate(
            mock_db,
            strategy_id=_STRATEGY_ID,
            config={"period": 20},
            metrics={"sharpe_ratio": 1.5},
        )

        # Assert
        assert result.stage == "discovery"
        assert result.strategy_id == _STRATEGY_ID
        assert mock_db.add.call_count == 2  # candidate + transition

    async def test_create_candidate_creates_initial_transition(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """create_candidate also creates an initial transition row."""

        # Arrange
        async def _flush() -> None:
            for call_args in mock_db.add.call_args_list:
                obj = call_args[0][0]
                if hasattr(obj, "strategy_id") and not hasattr(obj, "from_stage"):
                    obj.id = _CANDIDATE_ID

        mock_db.flush.side_effect = _flush

        # Act
        await service.create_candidate(
            mock_db,
            strategy_id=_STRATEGY_ID,
            config={"period": 20},
            metrics={"sharpe_ratio": 1.5},
        )

        # Assert: second add() call is the transition
        assert mock_db.add.call_count == 2
        transition_obj = mock_db.add.call_args_list[1][0][0]
        assert transition_obj.from_stage == ""
        assert transition_obj.to_stage == "discovery"
        assert transition_obj.reason == "Candidate created"


# ---------------------------------------------------------------------------
# Service tests: update_stage
# ---------------------------------------------------------------------------


class TestUpdateStage:
    """Tests for GraduationService.update_stage."""

    async def test_valid_transition_discovery_to_validation(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """discovery -> validation is a valid transition."""
        candidate = _make_candidate_row(stage="discovery")
        mock_db.get.return_value = candidate

        result = await service.update_stage(
            mock_db, _CANDIDATE_ID, new_stage="validation", reason="Metrics look good"
        )

        assert result.stage == "validation"
        # Verify transition row was added
        transition_obj = mock_db.add.call_args[0][0]
        assert transition_obj.from_stage == "discovery"
        assert transition_obj.to_stage == "validation"

    async def test_valid_transition_paper_review_to_discovery_demotion(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """paper_review -> discovery (demotion) is a valid transition."""
        candidate = _make_candidate_row(stage="paper_review")
        mock_db.get.return_value = candidate

        result = await service.update_stage(
            mock_db, _CANDIDATE_ID, new_stage="discovery", reason="Needs more research"
        )

        assert result.stage == "discovery"

    async def test_valid_transition_live_running_to_paused(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """live_running -> paused is a valid transition."""
        candidate = _make_candidate_row(stage="live_running")
        mock_db.get.return_value = candidate

        result = await service.update_stage(mock_db, _CANDIDATE_ID, new_stage="paused")

        assert result.stage == "paused"

    @pytest.mark.parametrize(
        "stage",
        [
            "discovery",
            "validation",
            "paper_candidate",
            "paper_running",
            "paper_review",
            "live_candidate",
            "live_running",
            "paused",
        ],
    )
    async def test_valid_transition_any_non_terminal_to_archived(
        self, service: GraduationService, mock_db: AsyncMock, stage: str
    ) -> None:
        """Any non-terminal stage -> archived is valid."""
        candidate = _make_candidate_row(stage=stage)
        mock_db.get.return_value = candidate

        result = await service.update_stage(mock_db, _CANDIDATE_ID, new_stage="archived")

        assert result.stage == "archived"

    async def test_invalid_transition_discovery_to_live_running_raises(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """discovery -> live_running is invalid and raises GraduationStageError."""
        candidate = _make_candidate_row(stage="discovery")
        mock_db.get.return_value = candidate

        with pytest.raises(GraduationStageError, match="Cannot transition"):
            await service.update_stage(mock_db, _CANDIDATE_ID, new_stage="live_running")

    async def test_invalid_transition_archived_to_anything_raises(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """archived -> any stage is invalid (terminal state)."""
        candidate = _make_candidate_row(stage="archived")
        mock_db.get.return_value = candidate

        with pytest.raises(GraduationStageError, match="terminal state"):
            await service.update_stage(mock_db, _CANDIDATE_ID, new_stage="discovery")

    async def test_update_stage_candidate_not_found_raises(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """update_stage raises ValueError for non-existent candidate."""
        mock_db.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            await service.update_stage(mock_db, uuid4(), new_stage="validation")


# ---------------------------------------------------------------------------
# Service tests: get_allowed_transitions
# ---------------------------------------------------------------------------


class TestGetAllowedTransitions:
    """Tests for GraduationService.get_allowed_transitions."""

    def test_discovery_allows_validation_and_archived(self, service: GraduationService) -> None:
        result = service.get_allowed_transitions("discovery")
        assert result == ["archived", "validation"]

    def test_archived_allows_nothing(self, service: GraduationService) -> None:
        result = service.get_allowed_transitions("archived")
        assert result == []

    def test_paper_review_allows_discovery_live_candidate_archived(
        self, service: GraduationService
    ) -> None:
        result = service.get_allowed_transitions("paper_review")
        assert result == ["archived", "discovery", "live_candidate"]

    def test_unknown_stage_returns_empty(self, service: GraduationService) -> None:
        result = service.get_allowed_transitions("nonexistent")
        assert result == []


# ---------------------------------------------------------------------------
# Service tests: list_candidates
# ---------------------------------------------------------------------------


class TestListCandidates:
    """Tests for GraduationService.list_candidates."""

    async def test_list_candidates_with_stage_filter(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """list_candidates filters by stage when provided."""
        c1 = _make_candidate_row(stage="discovery")
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [c1]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await service.list_candidates(mock_db, stage="discovery")

        assert len(result) == 1
        assert result[0].stage == "discovery"

    async def test_list_candidates_empty(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """list_candidates returns empty list when no candidates exist."""
        result = await service.list_candidates(mock_db)
        assert result == []


# ---------------------------------------------------------------------------
# Service tests: get_transitions
# ---------------------------------------------------------------------------


class TestGetTransitions:
    """Tests for GraduationService.get_transitions."""

    async def test_get_transitions_returns_ordered_audit_trail(
        self, service: GraduationService, mock_db: AsyncMock
    ) -> None:
        """get_transitions returns transitions ordered by created_at."""
        t1 = _make_transition_row(from_stage="", to_stage="discovery", seq=1)
        t2 = _make_transition_row(from_stage="discovery", to_stage="validation", seq=2)

        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [t1, t2]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        result = await service.get_transitions(mock_db, _CANDIDATE_ID)

        assert len(result) == 2
        assert result[0].to_stage == "discovery"
        assert result[1].to_stage == "validation"


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class TestGraduationAPI:
    """Tests for the graduation API endpoints."""

    async def test_list_candidates_returns_200(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /candidates returns 200 with paginated results."""
        response = await client_with_mock_db.get("/api/v1/graduation/candidates")

        assert response.status_code == 200
        body = response.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 0

    async def test_create_candidate_returns_201(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /candidates creates a candidate and returns 201."""
        # Arrange: make flush assign an id, make refresh populate fields
        candidate_id = uuid4()

        async def _fake_flush() -> None:
            for call_args in mock_db.add.call_args_list:
                obj = call_args[0][0]
                if hasattr(obj, "strategy_id") and not hasattr(obj, "from_stage"):
                    obj.id = candidate_id

        mock_db.flush.side_effect = _fake_flush

        async def _fake_refresh(obj: MagicMock) -> None:
            obj.id = candidate_id
            obj.strategy_id = _STRATEGY_ID
            obj.research_job_id = None
            obj.stage = "discovery"
            obj.config = {"period": 20}
            obj.metrics = {"sharpe_ratio": 1.5}
            obj.deployment_id = None
            obj.notes = None
            obj.promoted_by = None
            obj.promoted_at = None
            obj.created_at = datetime.now(UTC)
            obj.updated_at = datetime.now(UTC)

        mock_db.refresh.side_effect = _fake_refresh

        response = await client_with_mock_db.post(
            "/api/v1/graduation/candidates",
            json={
                "strategy_id": str(_STRATEGY_ID),
                "config": {"period": 20},
                "metrics": {"sharpe_ratio": 1.5},
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["id"] == str(candidate_id)
        assert body["stage"] == "discovery"

    async def test_get_candidate_not_found_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /candidates/{id} with non-existent ID returns 404."""
        mock_db.get.return_value = None

        response = await client_with_mock_db.get(f"/api/v1/graduation/candidates/{uuid4()}")

        assert response.status_code == 404

    async def test_get_candidate_returns_200(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /candidates/{id} with existing candidate returns 200."""
        candidate = _make_candidate_row()
        mock_db.get.return_value = candidate

        response = await client_with_mock_db.get(f"/api/v1/graduation/candidates/{_CANDIDATE_ID}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == str(_CANDIDATE_ID)
        assert body["stage"] == "discovery"

    @patch.object(GraduationService, "update_stage")
    async def test_update_stage_invalid_returns_422(
        self,
        mock_update: AsyncMock,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /candidates/{id}/stage with invalid transition returns 422."""
        mock_update.side_effect = GraduationStageError(
            "Cannot transition from 'discovery' to 'live_running'. "
            "Allowed transitions: ['archived', 'validation']"
        )
        # When the 422 handler looks up the candidate to report its current stage
        candidate = _make_candidate_row(stage="discovery")
        mock_db.get.return_value = candidate

        response = await client_with_mock_db.post(
            f"/api/v1/graduation/candidates/{_CANDIDATE_ID}/stage",
            json={"stage": "live_running"},
        )

        assert response.status_code == 422
        body = response.json()
        assert "allowed_transitions" in body["detail"]
        assert "archived" in body["detail"]["allowed_transitions"]
        assert "validation" in body["detail"]["allowed_transitions"]

    @patch.object(GraduationService, "update_stage")
    async def test_update_stage_valid_returns_200(
        self,
        mock_update: AsyncMock,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /candidates/{id}/stage with valid transition returns 200."""
        candidate = _make_candidate_row(stage="validation")
        mock_update.return_value = candidate

        async def _fake_refresh(obj: MagicMock) -> None:
            obj.id = _CANDIDATE_ID
            obj.strategy_id = _STRATEGY_ID
            obj.research_job_id = None
            obj.stage = "validation"
            obj.config = {"period": 20}
            obj.metrics = {"sharpe_ratio": 1.5}
            obj.deployment_id = None
            obj.notes = None
            obj.promoted_by = None
            obj.promoted_at = None
            obj.created_at = datetime.now(UTC)
            obj.updated_at = datetime.now(UTC)

        mock_db.refresh.side_effect = _fake_refresh

        response = await client_with_mock_db.post(
            f"/api/v1/graduation/candidates/{_CANDIDATE_ID}/stage",
            json={"stage": "validation", "reason": "Metrics look good"},
        )

        assert response.status_code == 200

    async def test_get_transitions_not_found_returns_404(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /candidates/{id}/transitions with non-existent candidate returns 404."""
        mock_db.get.return_value = None

        response = await client_with_mock_db.get(
            f"/api/v1/graduation/candidates/{uuid4()}/transitions"
        )

        assert response.status_code == 404

    async def test_get_transitions_returns_200(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """GET /candidates/{id}/transitions returns audit trail."""
        candidate = _make_candidate_row()
        t1 = _make_transition_row(from_stage="", to_stage="discovery", seq=1)
        t2 = _make_transition_row(from_stage="discovery", to_stage="validation", seq=2)

        # First call: session.get() returns candidate
        mock_db.get.return_value = candidate

        # Second call: session.execute() returns transitions
        mock_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [t1, t2]
        mock_result.scalars.return_value = mock_scalars
        mock_db.execute.return_value = mock_result

        response = await client_with_mock_db.get(
            f"/api/v1/graduation/candidates/{_CANDIDATE_ID}/transitions"
        )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        assert body["items"][0]["to_stage"] == "discovery"
        assert body["items"][1]["to_stage"] == "validation"
