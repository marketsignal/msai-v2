from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from msai.api import graduation as graduation_api
from msai.api import portfolio as portfolio_api
from msai.core.config import settings
from msai.core.database import get_db
from msai.main import app
from msai.models import Strategy
from msai.services.graduation_service import GraduationService
from msai.services.portfolio_service import PortfolioService
from msai.services.research_artifacts import ResearchArtifactService


@pytest.fixture(autouse=True)
def _reset_app_state() -> None:
    app.dependency_overrides.clear()


def test_graduation_candidate_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")

    research_root = tmp_path / "research"
    graduation_root = tmp_path / "graduation"
    strategies_root = tmp_path / "strategies"
    strategy_file = strategies_root / "example" / "mean_reversion.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text("class MeanReversionStrategy: pass\n")
    monkeypatch.setattr(settings, "strategies_root", strategies_root)

    artifact_service = ResearchArtifactService(root=research_root)
    graduation_service = GraduationService(root=graduation_root)
    monkeypatch.setattr(graduation_api, "artifact_service", artifact_service)
    monkeypatch.setattr(graduation_api, "graduation_service", graduation_service)

    artifact_service.promotions_root.mkdir(parents=True, exist_ok=True)
    (artifact_service.promotions_root / "promotion-1.json").write_text(
        json.dumps(
            {
                "id": "promotion-1",
                "report_id": "report-1",
                "strategy_id": "strategy-1",
                "strategy_name": "example.mean_reversion",
                "instruments": ["SPY.EQUS"],
                "config": {"lookback": 20},
                "selection": {"kind": "parameter_sweep", "metrics": {"sharpe": 1.8}},
                "paper_trading": True,
                "live_url": "/live?promotion_id=promotion-1",
            }
        )
    )

    strategy = Strategy(
        id="strategy-1",
        name="example.mean_reversion",
        file_path="example/mean_reversion.py",
        strategy_class="MeanReversionStrategy",
    )

    class _FakeSession:
        async def get(self, model, identifier: str):  # noqa: ANN001
            return strategy if model is Strategy and identifier == strategy.id else None

        async def commit(self) -> None:
            return None

    async def _override_db():
        yield _FakeSession()

    async def _fake_user_id(*_: object, **__: object) -> str:
        return "user-1"

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(graduation_api, "resolve_user_id_from_claims", _fake_user_id)

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}

        create_response = client.post(
            "/api/v1/graduation/candidates",
            headers=headers,
            json={"promotion_id": "promotion-1", "notes": "Paper candidate"},
        )
        assert create_response.status_code == 200
        candidate = create_response.json()
        assert candidate["stage"] == "paper_candidate"
        assert candidate["strategy_path"].endswith("example/mean_reversion.py")

        list_response = client.get("/api/v1/graduation/candidates", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()[0]["id"] == candidate["id"]

        update_response = client.post(
            f"/api/v1/graduation/candidates/{candidate['id']}/stage",
            headers=headers,
            json={"stage": "paper_running", "notes": "Started in paper"},
        )
        assert update_response.status_code == 200
        assert update_response.json()["stage"] == "paper_running"
        assert update_response.json()["notes"] == "Started in paper"


def test_portfolio_definition_and_run_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")

    graduation_service = GraduationService(root=tmp_path / "graduation")
    candidate = graduation_service.create_candidate(
        promotion={
            "id": "promotion-2",
            "report_id": "report-2",
            "strategy_id": "strategy-2",
            "strategy_name": "example.mean_reversion",
            "instruments": ["SPY.EQUS"],
            "config": {"lookback": 20},
            "selection": {"kind": "parameter_sweep", "metrics": {"sharpe": 1.9, "total_return": 0.12}},
            "paper_trading": True,
        },
        strategy_path="/repo/strategies/example/mean_reversion.py",
        created_by="user-1",
    )

    portfolio_service = PortfolioService(root=tmp_path / "portfolio", graduation_service=graduation_service)
    monkeypatch.setattr(portfolio_api, "portfolio_service", portfolio_service)

    class _FakeSession:
        async def commit(self) -> None:
            return None

    async def _override_db():
        yield _FakeSession()

    async def _fake_user_id(*_: object, **__: object) -> str:
        return "user-1"

    pool = AsyncMock()
    pool.enqueue_job.return_value = SimpleNamespace(job_id="portfolio-run-queue-id")

    async def _fake_get_redis_pool():
        return pool

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(portfolio_api, "resolve_user_id_from_claims", _fake_user_id)
    monkeypatch.setattr(portfolio_api, "get_redis_pool", _fake_get_redis_pool)

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}

        create_response = client.post(
            "/api/v1/portfolios",
            headers=headers,
            json={
                "name": "Core Portfolio",
                "description": "Research basket",
                "allocations": [{"candidate_id": candidate["id"], "weight": 1.0}],
                "objective": "maximize_sharpe",
                "base_capital": 1000000,
                "requested_leverage": 1.25,
                "downside_target": 0.1,
                "benchmark_symbol": "SPY.EQUS",
            },
        )
        assert create_response.status_code == 200
        portfolio = create_response.json()
        assert portfolio["allocations"][0]["candidate_id"] == candidate["id"]

        run_response = client.post(
            f"/api/v1/portfolios/{portfolio['id']}/runs",
            headers=headers,
            json={
                "start_date": "2026-03-31",
                "end_date": "2026-04-03",
                "max_parallelism": 2,
            },
        )
        assert run_response.status_code == 200
        run = run_response.json()
        assert run["status"] == "pending"
        pool.enqueue_job.assert_awaited_once()
        assert pool.enqueue_job.await_args.kwargs["run_id"] == run["id"]
        assert run["queue_name"] == settings.portfolio_queue_name
        assert run["queue_job_id"] == "portfolio-run-queue-id"

        list_response = client.get("/api/v1/portfolios", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()[0]["id"] == portfolio["id"]

        runs_response = client.get("/api/v1/portfolios/runs", headers=headers)
        assert runs_response.status_code == 200
        assert runs_response.json()[0]["id"] == run["id"]
