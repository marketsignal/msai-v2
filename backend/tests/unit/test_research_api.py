"""Unit tests for the research API endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.core.database import get_db
from msai.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRATEGY_ID = uuid4()
_JOB_ID = uuid4()


def _training_result(period: int = 20, score: float = 1.5) -> dict[str, Any]:
    metrics = {"sharpe_ratio": score, "total_trades": 10}
    return {
        "config": {"period": period},
        "metrics": metrics,
        "train_metrics": metrics,
        "objective_value": score,
        "selection_eligible": True,
        "selection_reason": None,
        "completed_full_run": True,
        "pruned": False,
        "error": None,
        "start_date": "2025-01-22",
        "end_date": "2025-01-23",
    }


def _report() -> dict[str, Any]:
    return {
        "selection": {
            "version": 1,
            "basis": "train",
            "scope": "exploratory",
            "policy": "best_training",
            "trial_index_kind": "sweep_result",
            "selected_trial_index": 0,
        },
        "objective": "sharpe",
        "results": [_training_result()],
        "start_date": "2025-01-22",
        "end_date": "2025-01-25",
        "search": {"holdout": {"train_start": "2025-01-22", "train_end": "2025-01-23"}},
    }


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
    row.results = _report() if status == "completed" else None
    row.selection = None
    row.discovery_eligible = False
    row.discovery_refusal_reason = None
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

    async def _refresh_identity(obj: Any) -> None:
        if obj.id is None:
            obj.id = uuid4()

    session.refresh = AsyncMock(side_effect=_refresh_identity)
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
    @patch("msai.api.research.settings")
    @patch("pathlib.Path.exists", return_value=True)
    async def test_submit_sweep_creates_job_returns_201(
        self,
        _mock_exists: MagicMock,
        mock_settings: MagicMock,
        mock_enqueue: AsyncMock,
        mock_pool: AsyncMock,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /sweeps creates a ResearchJob and returns 201 with job_id."""
        # Arrange: settings.strategies_root for path traversal defense
        mock_settings.strategies_root = Path("/app/strategies")
        mock_settings.research_queue_name = "msai:research"

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
        response = await client_with_mock_db.get("/api/v1/research/jobs?page=2&page_size=10")

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

        response = await client_with_mock_db.get(f"/api/v1/research/jobs/{uuid4()}")

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

        response = await client_with_mock_db.get(f"/api/v1/research/jobs/{_JOB_ID}")

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

        response = await client_with_mock_db.post(f"/api/v1/research/jobs/{uuid4()}/cancel")

        assert response.status_code == 404

    async def test_cancel_completed_job_returns_unchanged(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """POST /cancel on completed job returns it unchanged."""
        job = _make_job_row(status="completed")
        mock_db.get.return_value = job

        response = await client_with_mock_db.post(f"/api/v1/research/jobs/{_JOB_ID}/cancel")

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

        response = await client_with_mock_db.post(f"/api/v1/research/jobs/{_JOB_ID}/cancel")

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

    @pytest.mark.parametrize("job_status", ["pending", "running", "failed", "cancelled"])
    async def test_promote_non_completed_returns_409(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
        job_status: str,
    ) -> None:
        """POST /promotions on a running job returns 409."""
        job = _make_job_row(status=job_status)
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

    async def test_promote_stamps_instruments_from_job_config(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """Bug #3 (live-deploy-safety-trio): the API promotion endpoint
        MUST stamp `instruments` from `job.config["instruments"]` into
        the candidate's config before handing to GraduationService.
        Without this stamp, snapshot-binding at /start-portfolio would
        fail with BINDING_INSTRUMENTS_MISSING on every research-graduated
        candidate.
        """
        from unittest.mock import patch

        job = _make_job_row(status="completed")
        job.config = {
            "strategy_path": "/app/strategies/ema_cross.py",
            "instruments": ["AAPL.NASDAQ"],
        }
        job.best_config = {"period": 20}
        mock_db.get.return_value = job
        candidate_id = uuid4()

        async def _fake_refresh(obj: MagicMock) -> None:
            obj.id = candidate_id
            obj.stage = "discovery"

        mock_db.refresh.side_effect = _fake_refresh

        captured: dict[str, object] = {}

        async def _capture_create_candidate(_db: AsyncMock, **kwargs: object) -> MagicMock:
            captured.update(kwargs)
            stub = MagicMock()
            stub.id = candidate_id
            stub.stage = "discovery"
            return stub

        with patch(
            "msai.api.research._graduation_service.create_candidate",
            side_effect=_capture_create_candidate,
        ):
            response = await client_with_mock_db.post(
                "/api/v1/research/promotions",
                json={"research_job_id": str(_JOB_ID)},
            )

        assert response.status_code == 201
        # The config dict passed to create_candidate MUST carry the
        # stamped instruments from job.config — verifier expects them.
        assert captured["config"] == {"period": 20, "instruments": ["AAPL.NASDAQ"]}

    async def test_promote_stamps_instruments_for_trial_index_path(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        """Both promotion paths — best_config and trial_index — must
        stamp instruments. The trial path replaces `config` with the
        trial's params; the stamp must still land on top."""
        from unittest.mock import patch

        job = _make_job_row(status="completed")
        job.config = {
            "strategy_path": "/app/strategies/ema_cross.py",
            "instruments": ["MSFT.NASDAQ"],
        }
        trial = _make_trial_row(trial_number=3)
        trial.config = {"period": 50}
        trial.metrics = {"sharpe_ratio": 2.0}
        job.results["results"] = [_training_result() for _ in range(4)]
        job.results["results"][3] = _training_result(period=50, score=2.0)

        # mock_db.get returns job; mock_db.execute returns trial.
        mock_db.get.return_value = job
        trial_result = MagicMock()
        trial_result.scalar_one_or_none.return_value = trial
        mock_db.execute.return_value = trial_result

        candidate_id = uuid4()

        async def _fake_refresh(obj: MagicMock) -> None:
            obj.id = candidate_id
            obj.stage = "discovery"

        mock_db.refresh.side_effect = _fake_refresh

        captured: dict[str, object] = {}

        async def _capture_create_candidate(_db: AsyncMock, **kwargs: object) -> MagicMock:
            captured.update(kwargs)
            stub = MagicMock()
            stub.id = candidate_id
            stub.stage = "discovery"
            return stub

        with patch(
            "msai.api.research._graduation_service.create_candidate",
            side_effect=_capture_create_candidate,
        ):
            response = await client_with_mock_db.post(
                "/api/v1/research/promotions",
                json={"research_job_id": str(_JOB_ID), "trial_index": 3},
            )

        assert response.status_code == 201
        # Trial's config (period=50) + stamped instruments from the
        # parent job.
        assert captured["config"] == {"period": 50, "instruments": ["MSFT.NASDAQ"]}


class TestTrainingDiscovery:
    async def test_automatic_training_choice_survives_failed_holdout_diagnostic(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        job = _make_job_row(status="completed")
        job.results["results"][0]["holdout_error"] = "No reserved-period bars"
        job.best_metrics = {"sharpe_ratio": -99.0}
        mock_db.get.return_value = job
        with patch(
            "msai.api.research._graduation_service.create_candidate",
            new_callable=AsyncMock,
            return_value=_make_candidate_row(),
        ) as create:
            response = await client_with_mock_db.post(
                "/api/v1/research/promotions",
                json={
                    "research_job_id": str(_JOB_ID),
                },
            )
        assert response.status_code == 201
        assert create.call_args.kwargs["metrics"]["sharpe_ratio"] == 1.5
        assert create.call_args.kwargs["metrics"]["selection"]["policy"] == "best_training"

    @pytest.mark.parametrize("trial_index", [None, 0])
    async def test_legacy_selection_refuses_with_rerun_guidance(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
        trial_index: int | None,
    ) -> None:
        job = _make_job_row(status="completed")
        job.results = None
        mock_db.get.return_value = job
        response = await client_with_mock_db.post(
            "/api/v1/research/promotions",
            json={
                "research_job_id": str(_JOB_ID),
                "trial_index": trial_index,
            },
        )
        assert response.status_code == 409
        assert "rerun" in response.json()["detail"].lower()

    @pytest.mark.parametrize(
        "changes",
        [
            {"error": "training failed"},
            {"pruned": True},
            {"completed_full_run": False},
            {"selection_eligible": False, "selection_reason": "Too few training trades"},
            {"train_metrics": None},
            {"train_metrics": {}},
            {"objective_value": None},
            {"objective_value": float("inf")},
            {"train_metrics": {"sharpe_ratio": float("nan")}},
            {"train_metrics": {"total_trades": 10}},
        ],
    )
    async def test_canonical_invalid_training_refuses_even_if_trial_completed(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
        changes: dict[str, Any],
    ) -> None:
        job = _make_job_row(status="completed")
        job.results["results"][0].update(changes)
        mock_db.get.return_value = job
        trial_result = MagicMock()
        trial_result.scalar_one_or_none.return_value = _make_trial_row()
        mock_db.execute.return_value = trial_result
        response = await client_with_mock_db.post(
            "/api/v1/research/promotions",
            json={
                "research_job_id": str(_JOB_ID),
                "trial_index": 0,
            },
        )
        assert response.status_code == 409
        assert "rerun" in response.json()["detail"].lower()

    async def test_nonwinning_explicit_training_preserves_flat_metrics_and_provenance(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        job = _make_job_row(status="completed")
        job.config["instruments"] = ["AAPL.XNAS"]
        result = _training_result(period=50, score=0.5)
        result["holdout_error"] = "reserved diagnostic failed"
        job.results["results"].append(result)
        mock_db.get.return_value = job
        with patch(
            "msai.api.research._graduation_service.create_candidate",
            new_callable=AsyncMock,
            return_value=_make_candidate_row(),
        ) as create:
            response = await client_with_mock_db.post(
                "/api/v1/research/promotions",
                json={
                    "research_job_id": str(_JOB_ID),
                    "trial_index": 1,
                },
            )
        assert response.status_code == 201
        assert create.call_args.kwargs["config"] == {"period": 50, "instruments": ["AAPL.XNAS"]}
        metrics = create.call_args.kwargs["metrics"]
        assert metrics["sharpe_ratio"] == 0.5
        assert metrics["selection"] == {
            "version": 1,
            "research_job_id": str(_JOB_ID),
            "basis": "train",
            "scope": "exploratory",
            "policy": "explicit_trial",
            "trial_index_kind": "sweep_result",
            "selected_trial_index": 1,
            "train_start": "2025-01-22",
            "train_end": "2025-01-23",
        }
        assert "selection" not in create.call_args.kwargs["config"]

    async def test_earlier_walk_forward_explicit_choice_when_latest_has_no_winner(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        job = _make_job_row(status="completed", job_type="walk_forward")
        job.best_config = job.best_metrics = None
        job.results = {
            "objective": "sharpe",
            "selection": {
                "version": 1,
                "basis": "train",
                "scope": "exploratory",
                "policy": "latest_window_training",
                "trial_index_kind": "walk_forward_window",
                "selected_trial_index": None,
            },
            "windows": [
                {
                    "train_start": "2025-01-22",
                    "train_end": "2025-01-23",
                    "best_train_result": _training_result(),
                    "test_result": {"error": "no bars"},
                },
                {"best_train_result": None},
            ],
        }
        mock_db.get.return_value = job
        automatic = await client_with_mock_db.post(
            "/api/v1/research/promotions",
            json={
                "research_job_id": str(_JOB_ID),
            },
        )
        assert automatic.status_code == 409
        with patch(
            "msai.api.research._graduation_service.create_candidate",
            new_callable=AsyncMock,
            return_value=_make_candidate_row(),
        ) as create:
            explicit = await client_with_mock_db.post(
                "/api/v1/research/promotions",
                json={
                    "research_job_id": str(_JOB_ID),
                    "trial_index": 0,
                },
            )
        assert explicit.status_code == 201
        assert (
            create.call_args.kwargs["metrics"]["selection"]["trial_index_kind"]
            == "walk_forward_window"
        )
        assert create.call_args.kwargs["metrics"]["selection"]["policy"] == "explicit_trial"

    async def test_detail_exposes_selection_and_derived_eligibility(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        job = _make_job_row(status="completed")
        mock_db.get.return_value = job
        response = await client_with_mock_db.get(f"/api/v1/research/jobs/{_JOB_ID}")
        assert response.status_code == 200
        assert response.json()["selection"] == job.results["selection"]
        assert response.json()["discovery_eligible"] is True
        assert response.json()["discovery_refusal_reason"] is None

    async def test_list_legacy_job_remains_readable_with_unknown_selection(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        job = _make_job_row(status="completed")
        job.results = None
        count_result = MagicMock()
        count_result.scalar_one.return_value = 1
        jobs_result = MagicMock()
        jobs_result.scalars.return_value.all.return_value = [job]
        mock_db.execute.side_effect = [count_result, jobs_result]
        response = await client_with_mock_db.get("/api/v1/research/jobs")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["best_metrics"] == job.best_metrics
        assert item["selection"] is None
        assert item["discovery_eligible"] is False
        assert "rerun" in item["discovery_refusal_reason"].lower()

    @pytest.mark.parametrize("marker", [None, 2, True])
    async def test_unknown_contract_version_refuses(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
        marker: Any,
    ) -> None:
        job = _make_job_row(status="completed")
        job.results["selection"]["version"] = marker
        mock_db.get.return_value = job
        response = await client_with_mock_db.post(
            "/api/v1/research/promotions",
            json={
                "research_job_id": str(_JOB_ID),
            },
        )
        assert response.status_code == 409

    async def test_missing_canonical_result_refuses(
        self,
        mock_db: AsyncMock,
        client_with_mock_db: httpx.AsyncClient,
    ) -> None:
        job = _make_job_row(status="completed")
        job.results["results"] = []
        mock_db.get.return_value = job
        response = await client_with_mock_db.post(
            "/api/v1/research/promotions",
            json={
                "research_job_id": str(_JOB_ID),
                "trial_index": 0,
            },
        )
        assert response.status_code == 409
        assert "canonical training result" in response.json()["detail"]
