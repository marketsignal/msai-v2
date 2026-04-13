"""Unit tests for new Pydantic schemas: asset_universe, research, graduation, portfolio."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from msai.schemas.asset_universe import (
    AssetUniverseCreate,
    AssetUniverseListResponse,
    AssetUniverseResponse,
)
from msai.schemas.graduation import (
    GraduationCandidateCreate,
    GraduationCandidateListResponse,
    GraduationCandidateResponse,
    GraduationStageUpdate,
    GraduationTransitionListResponse,
    GraduationTransitionResponse,
)
from msai.schemas.portfolio import (
    PortfolioAllocationInput,
    PortfolioCreate,
    PortfolioListResponse,
    PortfolioResponse,
    PortfolioRunCreate,
    PortfolioRunListResponse,
    PortfolioRunResponse,
)
from msai.schemas.research import (
    ResearchJobDetailResponse,
    ResearchJobListResponse,
    ResearchJobResponse,
    ResearchPromotionRequest,
    ResearchPromotionResponse,
    ResearchSweepRequest,
    ResearchTrialResponse,
    ResearchWalkForwardRequest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime(2026, 4, 12, 10, 0, 0)


def _uuid() -> UUID:
    return uuid4()


# ---------------------------------------------------------------------------
# AssetUniverse schemas
# ---------------------------------------------------------------------------

class TestAssetUniverseCreate:
    def test_valid_create(self) -> None:
        schema = AssetUniverseCreate(
            symbol="AAPL",
            exchange="XNAS",
            asset_class="stocks",
        )
        assert schema.symbol == "AAPL"
        assert schema.resolution == "1m"  # default

    def test_custom_resolution(self) -> None:
        schema = AssetUniverseCreate(
            symbol="ES",
            exchange="CME",
            asset_class="futures",
            resolution="5m",
        )
        assert schema.resolution == "5m"

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            AssetUniverseCreate(symbol="AAPL", exchange="XNAS")  # type: ignore[call-arg]

    def test_symbol_max_length(self) -> None:
        with pytest.raises(ValidationError):
            AssetUniverseCreate(
                symbol="A" * 33,
                exchange="XNAS",
                asset_class="stocks",
            )

    def test_exchange_max_length(self) -> None:
        with pytest.raises(ValidationError):
            AssetUniverseCreate(
                symbol="AAPL",
                exchange="X" * 33,
                asset_class="stocks",
            )

    def test_asset_class_max_length(self) -> None:
        with pytest.raises(ValidationError):
            AssetUniverseCreate(
                symbol="AAPL",
                exchange="XNAS",
                asset_class="A" * 33,
            )


class TestAssetUniverseResponse:
    def test_from_orm_dict(self) -> None:
        uid = _uuid()
        now = _now()
        data = {
            "id": uid,
            "symbol": "AAPL",
            "exchange": "XNAS",
            "asset_class": "stocks",
            "resolution": "1m",
            "enabled": True,
            "last_ingested_at": None,
            "created_at": now,
            "updated_at": now,
        }
        resp = AssetUniverseResponse.model_validate(data)
        assert resp.id == uid
        assert resp.enabled is True
        assert resp.last_ingested_at is None


class TestAssetUniverseListResponse:
    def test_list_response_structure(self) -> None:
        resp = AssetUniverseListResponse(items=[], total=0)
        assert resp.items == []
        assert resp.total == 0


# ---------------------------------------------------------------------------
# Research schemas
# ---------------------------------------------------------------------------

class TestResearchSweepRequest:
    def test_valid_sweep(self) -> None:
        sid = _uuid()
        schema = ResearchSweepRequest(
            strategy_id=sid,
            instruments=["AAPL.SIM", "MSFT.SIM"],
            start_date=date(2024, 1, 1),
            end_date=date(2025, 1, 1),
            parameter_grid={"fast_period": [5, 10, 20]},
        )
        assert schema.strategy_id == sid
        assert schema.objective == "sharpe"
        assert schema.purge_days == 5

    def test_missing_parameter_grid_raises(self) -> None:
        with pytest.raises(ValidationError):
            ResearchSweepRequest(
                strategy_id=_uuid(),
                instruments=["AAPL.SIM"],
                start_date=date(2024, 1, 1),
                end_date=date(2025, 1, 1),
                # parameter_grid missing
            )  # type: ignore[call-arg]


class TestResearchWalkForwardRequest:
    def test_valid_walk_forward(self) -> None:
        schema = ResearchWalkForwardRequest(
            strategy_id=_uuid(),
            instruments=["AAPL.SIM"],
            start_date=date(2024, 1, 1),
            end_date=date(2025, 1, 1),
            parameter_grid={"fast_period": [5, 10]},
            train_days=252,
            test_days=63,
        )
        assert schema.train_days == 252
        assert schema.mode == "rolling"

    def test_missing_train_days_raises(self) -> None:
        with pytest.raises(ValidationError):
            ResearchWalkForwardRequest(
                strategy_id=_uuid(),
                instruments=["AAPL.SIM"],
                start_date=date(2024, 1, 1),
                end_date=date(2025, 1, 1),
                parameter_grid={"fast_period": [5]},
                test_days=63,
                # train_days missing
            )  # type: ignore[call-arg]


class TestResearchJobResponse:
    def test_from_orm_dict(self) -> None:
        uid = _uuid()
        sid = _uuid()
        now = _now()
        data = {
            "id": uid,
            "strategy_id": sid,
            "job_type": "sweep",
            "status": "running",
            "progress": 42,
            "progress_message": "Trial 42/100",
            "best_config": None,
            "best_metrics": None,
            "error_message": None,
            "started_at": now,
            "completed_at": None,
            "created_at": now,
        }
        resp = ResearchJobResponse.model_validate(data)
        assert resp.progress == 42
        assert resp.status == "running"


class TestResearchJobListResponse:
    def test_list_response_structure(self) -> None:
        resp = ResearchJobListResponse(items=[], total=0)
        assert resp.items == []
        assert resp.total == 0


class TestResearchTrialResponse:
    def test_from_orm_dict(self) -> None:
        uid = _uuid()
        now = _now()
        data = {
            "id": uid,
            "trial_number": 3,
            "config": {"fast_period": 10},
            "metrics": {"sharpe": 1.5},
            "status": "completed",
            "objective_value": 1.5,
            "backtest_id": _uuid(),
            "created_at": now,
        }
        resp = ResearchTrialResponse.model_validate(data)
        assert resp.trial_number == 3
        assert resp.objective_value == 1.5


class TestResearchJobDetailResponse:
    def test_inherits_job_fields(self) -> None:
        uid = _uuid()
        sid = _uuid()
        now = _now()
        data = {
            "id": uid,
            "strategy_id": sid,
            "job_type": "sweep",
            "status": "completed",
            "progress": 100,
            "progress_message": None,
            "best_config": {"fast_period": 10},
            "best_metrics": {"sharpe": 2.1},
            "error_message": None,
            "started_at": now,
            "completed_at": now,
            "created_at": now,
            "config": {"objective": "sharpe"},
            "results": {"total_trials": 50},
            "trials": [],
        }
        resp = ResearchJobDetailResponse.model_validate(data)
        assert resp.best_config == {"fast_period": 10}
        assert resp.trials == []


class TestResearchPromotionRequest:
    def test_valid_promotion(self) -> None:
        schema = ResearchPromotionRequest(
            research_job_id=_uuid(),
            trial_index=5,
            notes="Best sharpe from sweep",
        )
        assert schema.trial_index == 5


class TestResearchPromotionResponse:
    def test_response_fields(self) -> None:
        resp = ResearchPromotionResponse(
            candidate_id=_uuid(),
            stage="paper_review",
            message="Promoted to paper review",
        )
        assert resp.stage == "paper_review"


# ---------------------------------------------------------------------------
# Graduation schemas
# ---------------------------------------------------------------------------

class TestGraduationCandidateCreate:
    def test_valid_create(self) -> None:
        schema = GraduationCandidateCreate(
            strategy_id=_uuid(),
            config={"fast_period": 10},
            metrics={"sharpe": 2.0},
        )
        assert schema.research_job_id is None
        assert schema.notes is None

    def test_missing_config_raises(self) -> None:
        with pytest.raises(ValidationError):
            GraduationCandidateCreate(
                strategy_id=_uuid(),
                metrics={"sharpe": 2.0},
                # config missing
            )  # type: ignore[call-arg]


class TestGraduationStageUpdate:
    def test_valid_update(self) -> None:
        schema = GraduationStageUpdate(stage="paper_live", reason="Passed paper review")
        assert schema.stage == "paper_live"

    def test_missing_stage_raises(self) -> None:
        with pytest.raises(ValidationError):
            GraduationStageUpdate(reason="no stage")  # type: ignore[call-arg]


class TestGraduationCandidateResponse:
    def test_from_orm_dict(self) -> None:
        uid = _uuid()
        sid = _uuid()
        now = _now()
        data = {
            "id": uid,
            "strategy_id": sid,
            "research_job_id": None,
            "stage": "paper_review",
            "config": {"fast_period": 10},
            "metrics": {"sharpe": 2.0},
            "deployment_id": None,
            "notes": "Good candidate",
            "promoted_by": None,
            "promoted_at": None,
            "created_at": now,
            "updated_at": now,
        }
        resp = GraduationCandidateResponse.model_validate(data)
        assert resp.stage == "paper_review"
        assert resp.deployment_id is None


class TestGraduationCandidateListResponse:
    def test_list_response_structure(self) -> None:
        resp = GraduationCandidateListResponse(items=[], total=0)
        assert resp.items == []
        assert resp.total == 0


class TestGraduationTransitionResponse:
    def test_from_orm_dict(self) -> None:
        now = _now()
        data = {
            "id": 1,
            "candidate_id": _uuid(),
            "from_stage": "paper_review",
            "to_stage": "paper_live",
            "reason": "Approved by user",
            "transitioned_by": _uuid(),
            "created_at": now,
        }
        resp = GraduationTransitionResponse.model_validate(data)
        assert resp.from_stage == "paper_review"
        assert resp.to_stage == "paper_live"


class TestGraduationTransitionListResponse:
    def test_list_response_structure(self) -> None:
        resp = GraduationTransitionListResponse(items=[], total=0)
        assert resp.items == []
        assert resp.total == 0


# ---------------------------------------------------------------------------
# Portfolio schemas
# ---------------------------------------------------------------------------

class TestPortfolioAllocationInput:
    def test_valid_allocation(self) -> None:
        schema = PortfolioAllocationInput(candidate_id=_uuid(), weight=0.5)
        assert schema.weight == 0.5

    def test_weight_below_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioAllocationInput(candidate_id=_uuid(), weight=-0.1)

    def test_weight_above_one_raises(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioAllocationInput(candidate_id=_uuid(), weight=1.1)

    def test_weight_zero_valid(self) -> None:
        schema = PortfolioAllocationInput(candidate_id=_uuid(), weight=0.0)
        assert schema.weight == 0.0

    def test_weight_one_valid(self) -> None:
        schema = PortfolioAllocationInput(candidate_id=_uuid(), weight=1.0)
        assert schema.weight == 1.0


class TestPortfolioCreate:
    def test_valid_create(self) -> None:
        schema = PortfolioCreate(
            name="My Portfolio",
            objective="maximize_sharpe",
            base_capital=100000.0,
            allocations=[
                PortfolioAllocationInput(candidate_id=_uuid(), weight=0.6),
                PortfolioAllocationInput(candidate_id=_uuid(), weight=0.4),
            ],
        )
        assert len(schema.allocations) == 2
        assert schema.requested_leverage == 1.0  # default

    def test_name_max_length(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioCreate(
                name="A" * 129,
                objective="equal_weight",
                base_capital=50000.0,
                allocations=[],
            )

    def test_missing_objective_raises(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioCreate(
                name="Test",
                base_capital=50000.0,
                allocations=[],
            )  # type: ignore[call-arg]


class TestPortfolioResponse:
    def test_from_orm_dict(self) -> None:
        uid = _uuid()
        now = _now()
        data = {
            "id": uid,
            "name": "My Portfolio",
            "description": None,
            "objective": "maximize_sharpe",
            "base_capital": 100000.0,
            "requested_leverage": 1.0,
            "benchmark_symbol": "SPY",
            "account_id": None,
            "created_at": now,
            "updated_at": now,
        }
        resp = PortfolioResponse.model_validate(data)
        assert resp.name == "My Portfolio"
        assert resp.benchmark_symbol == "SPY"


class TestPortfolioListResponse:
    def test_list_response_structure(self) -> None:
        resp = PortfolioListResponse(items=[], total=0)
        assert resp.items == []
        assert resp.total == 0


class TestPortfolioRunCreate:
    def test_valid_create(self) -> None:
        schema = PortfolioRunCreate(
            start_date=date(2024, 1, 1),
            end_date=date(2025, 1, 1),
        )
        assert schema.max_parallelism is None

    def test_missing_dates_raises(self) -> None:
        with pytest.raises(ValidationError):
            PortfolioRunCreate(start_date=date(2024, 1, 1))  # type: ignore[call-arg]


class TestPortfolioRunResponse:
    def test_from_orm_dict(self) -> None:
        uid = _uuid()
        pid = _uuid()
        now = _now()
        data = {
            "id": uid,
            "portfolio_id": pid,
            "status": "completed",
            "metrics": {"sharpe": 1.8, "max_dd": -0.12},
            "report_path": "/data/reports/run_123.html",
            "start_date": date(2024, 1, 1),
            "end_date": date(2025, 1, 1),
            "created_at": now,
            "completed_at": now,
        }
        resp = PortfolioRunResponse.model_validate(data)
        assert resp.status == "completed"
        assert resp.metrics is not None
        assert resp.metrics["sharpe"] == 1.8


class TestPortfolioRunListResponse:
    def test_list_response_structure(self) -> None:
        resp = PortfolioRunListResponse(items=[], total=0)
        assert resp.items == []
        assert resp.total == 0
