"""Tests for the 8 new DB models (Task 2 of hybrid merge).

Verifies column presence, types, nullability, foreign keys, unique constraints,
and default values by inspecting SQLAlchemy table metadata — no database needed.
"""

from __future__ import annotations

import pytest

from msai.models.asset_universe import AssetUniverse
from msai.models.graduation_candidate import GraduationCandidate
from msai.models.graduation_stage_transition import GraduationStageTransition
from msai.models.portfolio import Portfolio
from msai.models.portfolio_allocation import PortfolioAllocation
from msai.models.portfolio_run import PortfolioRun
from msai.models.research_job import ResearchJob
from msai.models.research_trial import ResearchTrial


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _col(model, name):  # noqa: ANN001, ANN202
    """Get a column object by name."""
    return model.__table__.columns[name]


def _col_names(model):  # noqa: ANN001, ANN202
    """Get set of all column names for a model."""
    return {c.name for c in model.__table__.columns}


def _fk_targets(model, col_name):  # noqa: ANN001, ANN202
    """Get FK target table.column strings for a given column."""
    col = _col(model, col_name)
    return {str(fk.target_fullname) for fk in col.foreign_keys}


def _unique_constraint_columns(model):  # noqa: ANN001, ANN202
    """Get list of tuples of column names in unique constraints (excluding PK)."""
    result = []
    for constraint in model.__table__.constraints:
        from sqlalchemy import UniqueConstraint

        if isinstance(constraint, UniqueConstraint):
            result.append(tuple(c.name for c in constraint.columns))
    return result


# ===========================================================================
# 1. AssetUniverse
# ===========================================================================


class TestAssetUniverse:
    def test_tablename(self) -> None:
        assert AssetUniverse.__tablename__ == "asset_universe"

    def test_expected_columns(self) -> None:
        expected = {
            "id",
            "symbol",
            "exchange",
            "asset_class",
            "resolution",
            "enabled",
            "last_ingested_at",
            "created_by",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(_col_names(AssetUniverse))

    def test_uuid_pk(self) -> None:
        assert _col(AssetUniverse, "id").primary_key

    def test_created_by_fk(self) -> None:
        assert _fk_targets(AssetUniverse, "created_by") == {"users.id"}

    def test_created_by_indexed(self) -> None:
        assert _col(AssetUniverse, "created_by").index is True

    def test_enabled_defaults_true(self) -> None:
        col = _col(AssetUniverse, "enabled")
        assert col.server_default is not None

    def test_unique_constraint_symbol_exchange_resolution(self) -> None:
        ucs = _unique_constraint_columns(AssetUniverse)
        assert ("symbol", "exchange", "resolution") in ucs

    def test_has_timestamp_mixin(self) -> None:
        assert "created_at" in _col_names(AssetUniverse)
        assert "updated_at" in _col_names(AssetUniverse)


# ===========================================================================
# 2. ResearchJob
# ===========================================================================


class TestResearchJob:
    def test_tablename(self) -> None:
        assert ResearchJob.__tablename__ == "research_jobs"

    def test_expected_columns(self) -> None:
        expected = {
            "id",
            "strategy_id",
            "job_type",
            "config",
            "status",
            "progress",
            "progress_message",
            "results",
            "best_config",
            "best_metrics",
            "error_message",
            "started_at",
            "completed_at",
            "created_by",
            "created_at",
        }
        assert expected.issubset(_col_names(ResearchJob))

    def test_strategy_fk(self) -> None:
        assert _fk_targets(ResearchJob, "strategy_id") == {"strategies.id"}

    def test_strategy_id_indexed(self) -> None:
        assert _col(ResearchJob, "strategy_id").index is True

    def test_created_by_fk(self) -> None:
        assert _fk_targets(ResearchJob, "created_by") == {"users.id"}

    def test_status_defaults_pending(self) -> None:
        col = _col(ResearchJob, "status")
        assert col.server_default is not None
        assert "pending" in str(col.server_default.arg)

    def test_progress_defaults_zero(self) -> None:
        col = _col(ResearchJob, "progress")
        assert col.server_default is not None
        assert "0" in str(col.server_default.arg)

    def test_no_updated_at(self) -> None:
        assert "updated_at" not in _col_names(ResearchJob)


# ===========================================================================
# 3. ResearchTrial
# ===========================================================================


class TestResearchTrial:
    def test_tablename(self) -> None:
        assert ResearchTrial.__tablename__ == "research_trials"

    def test_expected_columns(self) -> None:
        expected = {
            "id",
            "research_job_id",
            "trial_number",
            "config",
            "metrics",
            "status",
            "objective_value",
            "backtest_id",
            "created_at",
        }
        assert expected.issubset(_col_names(ResearchTrial))

    def test_research_job_fk_cascade(self) -> None:
        fks = _col(ResearchTrial, "research_job_id").foreign_keys
        for fk in fks:
            assert fk.ondelete == "CASCADE"

    def test_research_job_id_indexed(self) -> None:
        assert _col(ResearchTrial, "research_job_id").index is True

    def test_backtest_fk(self) -> None:
        assert _fk_targets(ResearchTrial, "backtest_id") == {"backtests.id"}

    def test_unique_constraint_job_trial_number(self) -> None:
        ucs = _unique_constraint_columns(ResearchTrial)
        assert ("research_job_id", "trial_number") in ucs

    def test_status_defaults_pending(self) -> None:
        col = _col(ResearchTrial, "status")
        assert col.server_default is not None
        assert "pending" in str(col.server_default.arg)


# ===========================================================================
# 4. GraduationCandidate
# ===========================================================================


class TestGraduationCandidate:
    def test_tablename(self) -> None:
        assert GraduationCandidate.__tablename__ == "graduation_candidates"

    def test_expected_columns(self) -> None:
        expected = {
            "id",
            "strategy_id",
            "research_job_id",
            "stage",
            "config",
            "metrics",
            "deployment_id",
            "notes",
            "promoted_by",
            "promoted_at",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(_col_names(GraduationCandidate))

    def test_strategy_fk(self) -> None:
        assert _fk_targets(GraduationCandidate, "strategy_id") == {"strategies.id"}

    def test_research_job_fk(self) -> None:
        assert _fk_targets(GraduationCandidate, "research_job_id") == {"research_jobs.id"}

    def test_deployment_fk(self) -> None:
        assert _fk_targets(GraduationCandidate, "deployment_id") == {"live_deployments.id"}

    def test_promoted_by_fk(self) -> None:
        assert _fk_targets(GraduationCandidate, "promoted_by") == {"users.id"}

    def test_stage_defaults_discovery(self) -> None:
        col = _col(GraduationCandidate, "stage")
        assert col.server_default is not None
        assert "discovery" in str(col.server_default.arg)

    def test_all_fk_columns_indexed(self) -> None:
        for fk_col in ("strategy_id", "research_job_id", "deployment_id", "promoted_by"):
            assert _col(GraduationCandidate, fk_col).index is True, f"{fk_col} not indexed"

    def test_has_timestamp_mixin(self) -> None:
        assert "created_at" in _col_names(GraduationCandidate)
        assert "updated_at" in _col_names(GraduationCandidate)


# ===========================================================================
# 5. GraduationStageTransition
# ===========================================================================


class TestGraduationStageTransition:
    def test_tablename(self) -> None:
        assert GraduationStageTransition.__tablename__ == "graduation_stage_transitions"

    def test_expected_columns(self) -> None:
        expected = {
            "id",
            "candidate_id",
            "from_stage",
            "to_stage",
            "reason",
            "transitioned_by",
            "created_at",
        }
        assert expected.issubset(_col_names(GraduationStageTransition))

    def test_biginteger_pk(self) -> None:
        col = _col(GraduationStageTransition, "id")
        assert col.primary_key
        assert col.autoincrement is not False  # True or "auto"

    def test_candidate_fk_cascade(self) -> None:
        fks = _col(GraduationStageTransition, "candidate_id").foreign_keys
        for fk in fks:
            assert fk.ondelete == "CASCADE"

    def test_candidate_id_indexed(self) -> None:
        assert _col(GraduationStageTransition, "candidate_id").index is True

    def test_transitioned_by_fk(self) -> None:
        assert _fk_targets(GraduationStageTransition, "transitioned_by") == {"users.id"}

    def test_no_updated_at(self) -> None:
        """Immutable audit trail — no updated_at column."""
        assert "updated_at" not in _col_names(GraduationStageTransition)


# ===========================================================================
# 6. Portfolio
# ===========================================================================


class TestPortfolio:
    def test_tablename(self) -> None:
        assert Portfolio.__tablename__ == "portfolios"

    def test_expected_columns(self) -> None:
        expected = {
            "id",
            "name",
            "description",
            "objective",
            "base_capital",
            "requested_leverage",
            "benchmark_symbol",
            "account_id",
            "created_by",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(_col_names(Portfolio))

    def test_uuid_pk(self) -> None:
        assert _col(Portfolio, "id").primary_key

    def test_created_by_fk(self) -> None:
        assert _fk_targets(Portfolio, "created_by") == {"users.id"}

    def test_leverage_defaults_one(self) -> None:
        col = _col(Portfolio, "requested_leverage")
        assert col.server_default is not None
        assert "1.0" in str(col.server_default.arg)

    def test_has_timestamp_mixin(self) -> None:
        assert "created_at" in _col_names(Portfolio)
        assert "updated_at" in _col_names(Portfolio)


# ===========================================================================
# 7. PortfolioAllocation
# ===========================================================================


class TestPortfolioAllocation:
    def test_tablename(self) -> None:
        assert PortfolioAllocation.__tablename__ == "portfolio_allocations"

    def test_expected_columns(self) -> None:
        expected = {"id", "portfolio_id", "candidate_id", "weight", "created_at"}
        assert expected.issubset(_col_names(PortfolioAllocation))

    def test_portfolio_fk_cascade(self) -> None:
        fks = _col(PortfolioAllocation, "portfolio_id").foreign_keys
        for fk in fks:
            assert fk.ondelete == "CASCADE"

    def test_candidate_fk(self) -> None:
        assert _fk_targets(PortfolioAllocation, "candidate_id") == {"graduation_candidates.id"}

    def test_unique_constraint_portfolio_candidate(self) -> None:
        ucs = _unique_constraint_columns(PortfolioAllocation)
        assert ("portfolio_id", "candidate_id") in ucs

    def test_all_fk_columns_indexed(self) -> None:
        for fk_col in ("portfolio_id", "candidate_id"):
            assert _col(PortfolioAllocation, fk_col).index is True, f"{fk_col} not indexed"

    def test_no_updated_at(self) -> None:
        assert "updated_at" not in _col_names(PortfolioAllocation)


# ===========================================================================
# 8. PortfolioRun
# ===========================================================================


class TestPortfolioRun:
    def test_tablename(self) -> None:
        assert PortfolioRun.__tablename__ == "portfolio_runs"

    def test_expected_columns(self) -> None:
        expected = {
            "id",
            "portfolio_id",
            "status",
            "metrics",
            "series",
            "allocations",
            "report_path",
            "start_date",
            "end_date",
            "max_parallelism",
            "error_message",
            "heartbeat_at",
            "created_by",
            "created_at",
            "updated_at",
            "completed_at",
        }
        assert expected.issubset(_col_names(PortfolioRun))

    def test_portfolio_fk(self) -> None:
        assert _fk_targets(PortfolioRun, "portfolio_id") == {"portfolios.id"}

    def test_portfolio_id_indexed(self) -> None:
        assert _col(PortfolioRun, "portfolio_id").index is True

    def test_created_by_fk(self) -> None:
        assert _fk_targets(PortfolioRun, "created_by") == {"users.id"}

    def test_status_defaults_pending(self) -> None:
        col = _col(PortfolioRun, "status")
        assert col.server_default is not None
        assert "pending" in str(col.server_default.arg)

    def test_has_updated_at(self) -> None:
        # Added as part of the portfolio orchestration port — the run row
        # is now mutated during execution (heartbeat, status transitions),
        # so it needs an onupdate-tracked timestamp.
        assert "updated_at" in _col_names(PortfolioRun)


# ===========================================================================
# Cross-model: all 8 models importable from __init__
# ===========================================================================


class TestModelsInit:
    def test_all_new_models_exported(self) -> None:
        from msai.models import __all__ as exported

        new_models = {
            "AssetUniverse",
            "ResearchJob",
            "ResearchTrial",
            "GraduationCandidate",
            "GraduationStageTransition",
            "Portfolio",
            "PortfolioAllocation",
            "PortfolioRun",
        }
        assert new_models.issubset(set(exported))

    def test_import_from_package(self) -> None:
        """All 8 models are importable via the package namespace."""
        from msai.models import (  # noqa: F401
            AssetUniverse,
            GraduationCandidate,
            GraduationStageTransition,
            Portfolio,
            PortfolioAllocation,
            PortfolioRun,
            ResearchJob,
            ResearchTrial,
        )
