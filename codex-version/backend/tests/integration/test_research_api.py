from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from msai.api import research as research_api
from msai.core.config import settings
from msai.core.database import get_db
from msai.main import app
from msai.models import Strategy
from msai.services.research_artifacts import ResearchArtifactService
from msai.services.research_jobs import ResearchJobService


@pytest.fixture(autouse=True)
def _reset_research_api_state() -> None:
    app.dependency_overrides.clear()


def test_research_report_endpoints_and_promotion_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    monkeypatch.setattr(research_api, "artifact_service", ResearchArtifactService(root=tmp_path))

    _write_json(
        tmp_path / "mean-reversion-sweep.json",
        {
            "mode": "parameter_sweep",
            "generated_at": "2026-04-07T15:00:00Z",
            "objective": "sharpe",
            "strategy_path": "/repo/strategies/example/mean_reversion.py",
            "instruments": ["AAPL.EQUS"],
            "summary": {"total_runs": 2, "successful_runs": 2},
            "results": [
                {"config": {"lookback": 20}, "metrics": {"sharpe": 1.8}, "error": None},
                {"config": {"lookback": 10}, "metrics": {"sharpe": 1.2}, "error": None},
            ],
        },
    )
    _write_json(
        tmp_path / "breakout-wf.json",
        {
            "mode": "walk_forward",
            "generated_at": "2026-04-07T16:00:00Z",
            "objective": "sharpe",
            "strategy_path": "/repo/strategies/example/donchian_breakout.py",
            "instruments": ["ESM6.GLBX"],
            "summary": {"window_count": 1, "successful_test_windows": 1},
            "windows": [
                {
                    "train_start": "2026-01-01",
                    "train_end": "2026-02-01",
                    "test_start": "2026-02-02",
                    "test_end": "2026-02-10",
                    "best_train_result": {"config": {"channel_period": 20}, "metrics": {"sharpe": 1.4}, "error": None},
                    "test_result": {"metrics": {"sharpe": 1.1}, "error": None},
                }
            ],
        },
    )

    class _FakeSession:
        async def commit(self) -> None:
            return None

    async def _override_db():
        yield _FakeSession()

    async def _fake_user_id(*_: object, **__: object) -> str:
        return "user-1"

    async def _fake_resolve_strategy(*_: object, **__: object) -> Strategy:
        return Strategy(
            id="strategy-mean-reversion",
            name="example.mean_reversion",
            file_path="example/mean_reversion.py",
            strategy_class="MeanReversionStrategy",
        )

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(research_api, "resolve_user_id_from_claims", _fake_user_id)
    monkeypatch.setattr(research_api, "_resolve_strategy_for_report", _fake_resolve_strategy)

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}

        list_response = client.get("/api/v1/research/reports", headers=headers)
        assert list_response.status_code == 200
        assert {item["id"] for item in list_response.json()} == {
            "breakout-wf",
            "mean-reversion-sweep",
        }

        detail_response = client.get("/api/v1/research/reports/mean-reversion-sweep", headers=headers)
        assert detail_response.status_code == 200
        assert detail_response.json()["summary"]["best_config"] == {"lookback": 20}

        compare_response = client.post(
            "/api/v1/research/compare",
            headers=headers,
            json={"report_ids": ["mean-reversion-sweep", "breakout-wf"]},
        )
        assert compare_response.status_code == 200
        assert len(compare_response.json()["reports"]) == 2

        promotion_response = client.post(
            "/api/v1/research/promotions",
            headers=headers,
            json={"report_id": "mean-reversion-sweep", "result_index": 0, "paper_trading": True},
        )
        assert promotion_response.status_code == 200
        promotion = promotion_response.json()
        assert promotion["strategy_id"] == "strategy-mean-reversion"
        assert promotion["config"] == {"lookback": 20}

        loaded_response = client.get(
            f"/api/v1/research/promotions/{promotion['id']}",
            headers=headers,
        )
        assert loaded_response.status_code == 200
        assert loaded_response.json()["id"] == promotion["id"]


def test_research_job_submission_and_status_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    monkeypatch.setattr(research_api, "job_service", ResearchJobService(root=tmp_path / "jobs"))

    strategies_root = tmp_path / "strategies"
    strategy_file = strategies_root / "example" / "mean_reversion.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text("class MeanReversionStrategy: pass\n")
    monkeypatch.setattr(settings, "strategies_root", strategies_root)

    strategy = Strategy(
        id="strategy-mean-reversion",
        name="example.mean_reversion",
        file_path="example/mean_reversion.py",
        strategy_class="MeanReversionStrategy",
    )

    class _FakeSession:
        async def get(self, model, identifier: str):  # noqa: ANN001
            return strategy if model is Strategy and identifier == strategy.id else None

    async def _override_db():
        yield _FakeSession()

    async def _fake_canonicalize(*_: object, **__: object) -> list[str]:
        return ["SPY.EQUS"]

    pool = AsyncMock()
    pool.enqueue_job.return_value = SimpleNamespace(job_id="arq-sweep-1")

    async def _fake_get_redis_pool():
        return pool

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(research_api.instrument_service, "canonicalize_backtest_instruments", _fake_canonicalize)
    monkeypatch.setattr(research_api, "get_redis_pool", _fake_get_redis_pool)

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}
        response = client.post(
            "/api/v1/research/sweeps",
            headers=headers,
            json={
                "strategy_id": strategy.id,
                "instruments": ["SPY"],
                "start_date": "2026-04-01",
                "end_date": "2026-04-03",
                "base_config": {"lookback": 20},
                "parameter_grid": {"lookback": [10, 20], "zscore_threshold": [1.5, 2.0]},
                "objective": "sharpe",
                "max_parallelism": 2,
                "search_strategy": "successive_halving",
                "stage_fractions": [0.4, 1.0],
                "reduction_factor": 2,
                "min_trades": 5,
                "require_positive_return": True,
            },
        )

        assert response.status_code == 200
        job_id = response.json()["job_id"]

        pool.enqueue_job.assert_awaited_once()
        queued_kwargs = pool.enqueue_job.await_args.kwargs
        assert queued_kwargs["job_id"] == job_id
        assert queued_kwargs["job_type"] == "parameter_sweep"
        assert queued_kwargs["payload"]["instruments"] == ["SPY.EQUS"]
        assert queued_kwargs["payload"]["search_strategy"] == "successive_halving"
        assert queued_kwargs["payload"]["stage_fractions"] == [0.4, 1.0]
        assert queued_kwargs["payload"]["min_trades"] == 5
        assert queued_kwargs["payload"]["require_positive_return"] is True

        list_response = client.get("/api/v1/research/jobs", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()[0]["id"] == job_id

        detail_response = client.get(f"/api/v1/research/jobs/{job_id}", headers=headers)
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["strategy_name"] == "example.mean_reversion"
        assert detail["request"]["max_parallelism"] == 2
        assert detail["request"]["search_strategy"] == "successive_halving"
        assert detail["queue_job_id"] == "arq-sweep-1"


def test_walk_forward_job_submission_queues_expected_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    monkeypatch.setattr(research_api, "job_service", ResearchJobService(root=tmp_path / "jobs"))

    strategies_root = tmp_path / "strategies"
    strategy_file = strategies_root / "example" / "mean_reversion.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text("class MeanReversionStrategy: pass\n")
    monkeypatch.setattr(settings, "strategies_root", strategies_root)

    strategy = Strategy(
        id="strategy-mean-reversion",
        name="example.mean_reversion",
        file_path="example/mean_reversion.py",
        strategy_class="MeanReversionStrategy",
    )

    class _FakeSession:
        async def get(self, model, identifier: str):  # noqa: ANN001
            return strategy if model is Strategy and identifier == strategy.id else None

    async def _override_db():
        yield _FakeSession()

    async def _fake_canonicalize(*_: object, **__: object) -> list[str]:
        return ["SPY.EQUS"]

    pool = AsyncMock()
    pool.enqueue_job.return_value = SimpleNamespace(job_id="arq-wf-1")

    async def _fake_get_redis_pool():
        return pool

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(research_api.instrument_service, "canonicalize_backtest_instruments", _fake_canonicalize)
    monkeypatch.setattr(research_api, "get_redis_pool", _fake_get_redis_pool)

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}
        response = client.post(
            "/api/v1/research/walk-forward",
            headers=headers,
            json={
                "strategy_id": strategy.id,
                "instruments": ["SPY"],
                "start_date": "2026-04-01",
                "end_date": "2026-04-03",
                "base_config": {"lookback": 20},
                "parameter_grid": {"lookback": [10, 20], "zscore_threshold": [1.5, 2.0]},
                "objective": "sharpe",
                "max_parallelism": 2,
                "train_days": 2,
                "test_days": 1,
                "step_days": 1,
                "mode": "rolling",
            },
        )

        assert response.status_code == 200
        job_id = response.json()["job_id"]

        pool.enqueue_job.assert_awaited_once()
        queued_kwargs = pool.enqueue_job.await_args.kwargs
        assert queued_kwargs["job_id"] == job_id
        assert queued_kwargs["job_type"] == "walk_forward"
        assert queued_kwargs["payload"]["instruments"] == ["SPY.EQUS"]
        assert queued_kwargs["payload"]["train_days"] == 2
        assert queued_kwargs["payload"]["test_days"] == 1
        assert queued_kwargs["payload"]["step_days"] == 1
        assert queued_kwargs["payload"]["mode"] == "rolling"


def test_research_job_cancel_and_retry_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    job_service = ResearchJobService(root=tmp_path / "jobs")
    monkeypatch.setattr(research_api, "job_service", job_service)

    strategies_root = tmp_path / "strategies"
    strategy_file = strategies_root / "example" / "mean_reversion.py"
    strategy_file.parent.mkdir(parents=True, exist_ok=True)
    strategy_file.write_text("class MeanReversionStrategy: pass\n")
    monkeypatch.setattr(settings, "strategies_root", strategies_root)

    strategy = Strategy(
        id="strategy-mean-reversion",
        name="example.mean_reversion",
        file_path="example/mean_reversion.py",
        strategy_class="MeanReversionStrategy",
    )

    class _FakeSession:
        async def get(self, model, identifier: str):  # noqa: ANN001
            return strategy if model is Strategy and identifier == strategy.id else None

    async def _override_db():
        yield _FakeSession()

    async def _fake_canonicalize(*_: object, **__: object) -> list[str]:
        return ["SPY.EQUS"]

    pool = AsyncMock()
    pool.enqueue_job.return_value = SimpleNamespace(job_id="arq-job-1")

    async def _fake_get_redis_pool():
        return pool

    app.dependency_overrides[get_db] = _override_db
    monkeypatch.setattr(research_api.instrument_service, "canonicalize_backtest_instruments", _fake_canonicalize)
    monkeypatch.setattr(research_api, "get_redis_pool", _fake_get_redis_pool)

    with TestClient(app) as client:
        headers = {"X-API-Key": "msai-test-key"}
        response = client.post(
            "/api/v1/research/sweeps",
            headers=headers,
            json={
                "strategy_id": strategy.id,
                "instruments": ["SPY"],
                "start_date": "2026-04-01",
                "end_date": "2026-04-03",
                "base_config": {},
                "parameter_grid": {"lookback": [10, 20]},
            },
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]

        cancel_response = client.post(f"/api/v1/research/jobs/{job_id}/cancel", headers=headers)
        assert cancel_response.status_code == 200
        assert cancel_response.json()["status"] == "cancelled"
        pool.zrem.assert_awaited()
        pool.delete.assert_awaited()

        failed_job = job_service.create_job(
            job_type="parameter_sweep",
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            strategy_path=str(strategy_file),
            request={
                "strategy_id": strategy.id,
                "strategy_name": strategy.name,
                "strategy_path": str(strategy_file),
                "instruments": ["SPY.EQUS"],
                "start_date": "2026-04-01",
                "end_date": "2026-04-03",
                "base_config": {},
                "parameter_grid": {"lookback": [10, 20]},
                "objective": "sharpe",
                "max_parallelism": None,
                "search_strategy": "auto",
                "stage_fractions": None,
                "reduction_factor": 2,
                "min_trades": None,
                "require_positive_return": False,
                "holdout_fraction": None,
                "holdout_days": None,
                "purge_days": 5,
            },
        )
        job_service.mark_failed(failed_job["id"], error_message="boom")
        pool.enqueue_job.reset_mock()
        pool.enqueue_job.return_value = SimpleNamespace(job_id="arq-job-2")

        retry_response = client.post(f"/api/v1/research/jobs/{failed_job['id']}/retry", headers=headers)
        assert retry_response.status_code == 200
        retried_job_id = retry_response.json()["job_id"]
        assert retried_job_id != failed_job["id"]

        retried_detail = client.get(f"/api/v1/research/jobs/{retried_job_id}", headers=headers)
        assert retried_detail.status_code == 200
        assert retried_detail.json()["queue_job_id"] == "arq-job-2"


def test_research_capacity_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")

    async def _fake_get_redis_pool():
        return object()

    async def _fake_describe_system_capacity(_: object) -> dict[str, object]:
        return {
            "compute_slots": {
                "limit": 8,
                "used": 3,
                "available": 5,
                "active_leases": 1,
                "leases": [
                    {
                        "lease_id": "lease-1",
                        "job_kind": "research",
                        "job_id": "job-1",
                        "slot_count": 3,
                        "updated_at": "2026-04-08T12:00:00Z",
                    }
                ],
            },
            "workers": {
                "total_active_workers": 2,
                "total_capacity": 3,
                "workers": [
                    {
                        "worker_id": "worker-1",
                        "worker_role": "research-worker",
                        "queue_name": settings.research_queue_name,
                        "max_jobs": 2,
                    }
                ],
                "queues": [
                    {
                        "queue_name": settings.research_queue_name,
                        "worker_role": "research-worker",
                        "active_workers": 1,
                        "total_capacity": 2,
                        "max_jobs_per_worker": 2,
                        "queued_jobs": 4,
                    }
                ],
            },
        }

    monkeypatch.setattr(research_api, "get_redis_pool", _fake_get_redis_pool)
    monkeypatch.setattr(research_api, "describe_system_capacity", _fake_describe_system_capacity)

    with TestClient(app) as client:
        response = client.get("/api/v1/research/capacity", headers={"X-API-Key": "msai-test-key"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["compute_slots"]["used"] == 3
    assert payload["workers"]["queues"][0]["queued_jobs"] == 4


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload))
