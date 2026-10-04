"""Actual engine/worker selection controls; only execution and DB transport are fake."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import optuna
import pytest
from optuna.samplers import BaseSampler
from optuna.storages import JournalStorage
from optuna.storages.journal import JournalFileBackend

from msai.core.config import settings
from msai.services.research_engine import (
    ResearchEngine,
    rank_results,
    resolve_optuna_study_name,
    resolve_train_holdout_split,
)
from msai.workers import research_job as worker


def metrics(score: Any = 3, *, trades: Any = 10, total_return: Any = 0.1) -> dict[str, Any]:
    return {
        "sharpe_ratio": score,
        "sortino_ratio": score,
        "total_return": total_return,
        "num_trades": trades,
        "max_drawdown": -0.1,
        "win_rate": 0.5,
    }


class Runner:
    def __init__(self, schedule: dict[tuple[str, str, str], Any]) -> None:
        self.schedule = schedule
        self.calls: list[tuple[str, str, str]] = []

    def run(self, **kwargs: Any) -> SimpleNamespace:
        key = (kwargs["start_date"], kwargs["end_date"], kwargs["strategy_config"]["choice"])
        self.calls.append(key)
        value = self.schedule[key]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(metrics=deepcopy(value))


class OrderedSampler(BaseSampler):
    def infer_relative_search_space(self, study: Any, trial: Any) -> dict[str, Any]:
        return {}

    def sample_relative(self, study: Any, trial: Any, search_space: Any) -> dict[str, Any]:
        return {}

    def sample_independent(
        self, study: Any, trial: Any, param_name: str, param_distribution: Any
    ) -> Any:
        return param_distribution.choices[trial.number % len(param_distribution.choices)]


@pytest.fixture(autouse=True)
def isolated_optuna(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "data_root", tmp_path)
    monkeypatch.setattr(settings, "optuna_enabled", True)
    monkeypatch.setattr(settings, "optuna_max_trials", 2)
    monkeypatch.setattr("optuna.samplers.TPESampler", OrderedSampler)


def sweep(
    train: dict[str, Any],
    holdout: dict[str, Any] | None = None,
    *,
    mode: str = "grid",
    min_trades: int | None = None,
    positive: bool = False,
) -> tuple[dict[str, Any], Runner]:
    train_end = "2024-03-05" if holdout is not None else "2024-03-31"
    schedule = {("2024-01-01", train_end, label): value for label, value in train.items()}
    if holdout is not None:
        schedule.update({("2024-03-11", "2024-03-31", label): v for label, v in holdout.items()})
        schedule.update({("2024-01-01", "2024-03-31", label): metrics(99) for label in train})
    runner = Runner(schedule)
    report = ResearchEngine(runner=runner).run_parameter_sweep(
        strategy_path="/offline/strategy.py",
        base_config={},
        parameter_grid={"choice": ["A", "B"]},
        instruments=["AAPL.SIM"],
        start_date="2024-01-01",
        end_date="2024-03-31",
        data_path=Path("/offline/data"),
        search_strategy=mode,
        stage_fractions=[1.0],
        min_trades=min_trades,
        require_positive_return=positive,
        holdout_days=21 if holdout is not None else None,
    )
    return report, runner


def feedback(report: dict[str, Any]) -> list[tuple[str, float | None, str]]:
    study = optuna.load_study(
        study_name=report["search"]["study_name"],
        storage=JournalStorage(JournalFileBackend(report["search"]["storage_path"])),
    )
    return [(t.params["choice"], t.value, t.state.name) for t in study.trials]


@pytest.mark.parametrize("mode", ["grid", "successive_halving", "optuna"])
@pytest.mark.parametrize("reserved", [metrics(-8), metrics(80), RuntimeError("diagnostic failed")])
def test_reserved_values_or_failure_never_change_training_choice(mode: str, reserved: Any) -> None:
    # Changing the diagnostic must not rerank/tell or evaluate the nonwinning configuration.
    report, runner = sweep(
        {"A": metrics(3), "B": metrics(1)}, {"A": reserved, "B": metrics(100)}, mode=mode
    )
    assert report["summary"]["best_result"]["config"] == {"choice": "A"}
    assert [r["config"]["choice"] for r in report["results"]] == ["A", "B"]
    best = report["summary"]["best_result"]
    assert best["metrics"] == best["train_metrics"] == metrics(3)
    assert best["selection_basis"] == "train"
    assert best["selection_eligible"] is True
    assert best["objective_value"] == 3
    assert report["summary"]["holdout_evaluated_runs"] == 1
    assert [c for c in runner.calls if c[0] == "2024-03-11"] == [("2024-03-11", "2024-03-31", "A")]
    assert report["selection"] == {
        "version": 1,
        "basis": "train",
        "scope": "exploratory",
        "policy": "best_training",
        "trial_index_kind": "sweep_result",
        "selected_trial_index": 0,
    }
    if isinstance(reserved, Exception):
        assert best["holdout_error"] == "diagnostic failed"
        assert best["holdout_metrics"] is None
    else:
        assert best["holdout_metrics"] == reserved
    if mode == "optuna":
        assert feedback(report) == [("A", 3.0, "COMPLETE"), ("B", 1.0, "COMPLETE")]


@pytest.mark.parametrize("mode", ["grid", "successive_halving", "optuna"])
def test_tied_training_scores_keep_candidate_order(mode: str) -> None:
    report, _ = sweep(
        {"A": metrics(3), "B": metrics(3)}, {"A": metrics(-1), "B": metrics(9)}, mode=mode
    )
    assert [r["config"]["choice"] for r in report["results"]] == ["A", "B"]
    assert report["summary"]["best_result"]["config"] == {"choice": "A"}


@pytest.mark.parametrize("mode", ["grid", "successive_halving", "optuna"])
@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        metrics(None),
        metrics(float("nan")),
        metrics(float("inf")),
        RuntimeError("train failed"),
    ],
)
def test_unusable_training_cannot_win_or_send_zero_feedback(mode: str, bad: Any) -> None:
    report, _ = sweep({"A": bad, "B": metrics(-1)}, mode=mode)
    assert report["summary"]["best_result"]["config"] == {"choice": "B"}
    rejected = next(r for r in report["results"] if r["config"]["choice"] == "A")
    assert rejected["selection_eligible"] is False
    assert rejected["selection_reason"]
    assert rejected["objective_value"] is None
    if mode == "optuna":
        assert feedback(report) == [("A", None, "FAIL"), ("B", -1.0, "COMPLETE")]


@pytest.mark.parametrize("mode", ["grid", "successive_halving", "optuna"])
@pytest.mark.parametrize("bad", [metrics(4, trades=0), metrics(4, total_return=-1)])
def test_full_training_filters_reject_high_score_consistently(mode: str, bad: Any) -> None:
    report, _ = sweep({"A": bad, "B": metrics(1)}, mode=mode, min_trades=10, positive=True)
    assert report["summary"]["best_result"]["config"] == {"choice": "B"}
    rejected = next(r for r in report["results"] if r["config"]["choice"] == "A")
    assert rejected["selection_eligible"] is False
    assert rejected["pruned"] is True
    if mode == "optuna":
        assert feedback(report) == [("A", None, "PRUNED"), ("B", 1.0, "COMPLETE")]


@pytest.mark.parametrize("mode", ["grid", "successive_halving", "optuna"])
def test_all_ineligible_training_has_no_diagnostic_execution(mode: str) -> None:
    report, runner = sweep(
        {"A": metrics(4, trades=0), "B": metrics(1, trades=0)},
        {"A": metrics(9), "B": metrics(8)},
        mode=mode,
        min_trades=10,
    )
    assert report["summary"]["best_result"] is None
    assert report["summary"]["full_period_result"] is None
    assert report["summary"]["holdout_evaluated_runs"] == 0
    assert report["selection"]["selected_trial_index"] is None
    assert all(c[1] == "2024-03-05" for c in runner.calls)


def test_optuna_new_invocation_cannot_load_contaminated_or_previous_history() -> None:
    old_name = resolve_optuna_study_name(
        study_key=None,
        strategy_path="/offline/strategy.py",
        instruments=["AAPL.SIM"],
        start_date="2024-01-01",
        end_date="2024-03-31",
        objective="sharpe",
    )
    settings.optuna_root.mkdir(parents=True)
    old_storage = JournalStorage(
        JournalFileBackend(str(settings.optuna_root / f"{old_name}.journal"))
    )
    old = optuna.create_study(study_name=old_name, storage=old_storage)
    old.add_trial(optuna.trial.create_trial(value=999))
    first, _ = sweep({"A": metrics(3), "B": metrics(1)}, mode="optuna")
    second, _ = sweep({"A": metrics(1), "B": metrics(4)}, mode="optuna")
    assert len({old_name, first["search"]["study_name"], second["search"]["study_name"]}) == 3
    assert feedback(first) == [("A", 3.0, "COMPLETE"), ("B", 1.0, "COMPLETE")]
    assert feedback(second) == [("A", 1.0, "COMPLETE"), ("B", 4.0, "COMPLETE")]


@pytest.mark.parametrize("holdout_days,purge_days", [(2, 3), (5, 0)])
def test_impossible_explicit_holdout_refuses_instead_of_unsplit_run(
    holdout_days: int, purge_days: int
) -> None:
    with pytest.raises(ValueError, match="holdout.*purge|training range"):
        resolve_train_holdout_split(
            start_date="2024-01-01",
            end_date="2024-01-05",
            holdout_fraction=None,
            holdout_days=holdout_days,
            purge_days=purge_days,
        )


def test_automatic_split_discloses_origin_and_actual_dates() -> None:
    split = resolve_train_holdout_split(
        start_date="2024-01-01",
        end_date="2024-12-31",
        holdout_fraction=None,
        holdout_days=None,
        purge_days=5,
    )
    assert split is not None
    assert split["origin"] == "automatic"
    assert split["train_end"] == "2024-10-13"
    assert split["holdout_start"] == "2024-10-19"
    assert split["holdout_end"] == "2024-12-31"


def walk_report(*, test_score: Any = 1, latest_train_failure: bool = False) -> dict[str, Any]:
    schedule: dict[tuple[str, str, str], Any] = {
        ("2024-01-01", "2024-01-30", "A"): metrics(3),
        ("2024-01-01", "2024-01-30", "B"): metrics(1),
        ("2024-01-31", "2024-02-09", "A"): metrics(9),
        ("2024-01-11", "2024-02-09", "A"): metrics(1),
        ("2024-01-11", "2024-02-09", "B"): metrics(3),
        ("2024-02-10", "2024-02-19", "B"): test_score
        if isinstance(test_score, (Exception, dict))
        else metrics(test_score),
    }
    if latest_train_failure:
        schedule[("2024-01-11", "2024-02-09", "A")] = RuntimeError("train failed")
        schedule[("2024-01-11", "2024-02-09", "B")] = RuntimeError("train failed")
    return ResearchEngine(runner=Runner(schedule)).run_walk_forward(
        strategy_path="/offline/strategy.py",
        base_config={},
        parameter_grid={"choice": ["A", "B"]},
        instruments=["AAPL.SIM"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 19),
        train_days=30,
        test_days=10,
        step_days=10,
        data_path=Path("/offline/data"),
        search_strategy="grid",
    )


class Session:
    def __init__(self) -> None:
        self.job = SimpleNamespace()
        self.trials: list[Any] = []
        self.committed = False

    async def __aenter__(self) -> Session:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    async def get(self, model: Any, job_id: str) -> SimpleNamespace:
        return self.job

    def add(self, trial: Any) -> None:
        self.trials.append(trial)

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("test_score", [1, 99, RuntimeError("test failed")])
async def test_real_finalizer_persists_latest_training_despite_test_values_or_failure(
    monkeypatch: pytest.MonkeyPatch, test_score: Any
) -> None:
    report = walk_report(test_score=test_score)
    session = Session()
    monkeypatch.setattr(worker, "async_session_factory", lambda: session)
    await worker._finalize_job("offline-job", report)
    assert session.committed is True
    assert session.job.best_config == {"choice": "B"}
    assert session.job.best_metrics == metrics(3)
    assert [(t.trial_number, t.config, t.objective_value, t.status) for t in session.trials] == [
        (0, {"choice": "A"}, 3.0, "completed"),
        (1, {"choice": "B"}, 3.0, "completed"),
    ]
    assert all(t.metrics == metrics(3) for t in session.trials)
    assert report["summary"]["best_result"]["config"] == {"choice": "B"}
    assert report["selection"]["selected_trial_index"] == 1
    assert report["selection"]["policy"] == "latest_window_training"
    if isinstance(test_score, Exception):
        assert session.job.results["windows"][1]["test_result"]["error"] == "test failed"


@pytest.mark.asyncio
async def test_latest_invalid_training_never_falls_back_to_earlier_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = walk_report(latest_train_failure=True)
    session = Session()
    monkeypatch.setattr(worker, "async_session_factory", lambda: session)
    await worker._finalize_job("offline-job", report)
    assert session.job.best_config is None
    assert session.job.best_metrics is None
    assert report["selection"]["selected_trial_index"] is None
    assert report["windows"][0]["best_train_result"]["selection_eligible"] is True
    assert session.trials[1].status == "failed"
    assert session.trials[1].metrics is None
    assert session.trials[1].objective_value is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad,want",
    [(metrics(4, trades=0), "pruned"), ({}, "failed"), (RuntimeError("train failed"), "failed")],
)
async def test_real_finalizer_keeps_ineligible_sweep_status_and_null_objective(
    monkeypatch: pytest.MonkeyPatch, bad: Any, want: str
) -> None:
    report, _ = sweep({"A": bad, "B": metrics(1)}, min_trades=10)
    session = Session()
    monkeypatch.setattr(worker, "async_session_factory", lambda: session)
    await worker._finalize_job("offline-job", report)
    rejected = next(t for t in session.trials if t.config == {"choice": "A"})
    assert rejected.status == want
    if want == "failed":
        assert rejected.objective_value is None
    assert session.job.best_config == {"choice": "B"}


def test_ranking_prefers_training_metrics_and_ignores_diagnostic_failure() -> None:
    rows = [
        {
            "config": {"choice": "A"},
            "metrics": metrics(-8),
            "train_metrics": metrics(3),
            "holdout_error": "failed",
            "completed_full_run": True,
        },
        {
            "config": {"choice": "B"},
            "metrics": metrics(9),
            "train_metrics": metrics(1),
            "completed_full_run": True,
        },
    ]
    assert rank_results(rows)[0]["config"] == {"choice": "A"}


def test_nonfinite_metrics_remain_persistable_as_unavailable_evidence() -> None:
    # PostgreSQL JSON rejects NaN/Infinity; rejected or diagnostic metrics must still persist.
    report, _ = sweep(
        {"A": metrics(float("nan")), "B": metrics(1)},
        {"A": metrics(9), "B": metrics(float("inf"))},
    )
    encoded = json.dumps(report, allow_nan=False)
    assert '"selection_eligible": false' in encoded
    assert report["results"][1]["train_metrics"]["sharpe_ratio"] is None
    assert report["summary"]["best_result"]["holdout_metrics"]["sharpe_ratio"] is None


def test_walk_forward_discloses_automatic_split_within_training_slice() -> None:
    runner = MagicMock()
    runner.run.return_value = SimpleNamespace(metrics=metrics(3))
    report = ResearchEngine(runner=runner).run_walk_forward(
        strategy_path="/offline/strategy.py",
        base_config={},
        parameter_grid={},
        instruments=["AAPL.SIM"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 11, 5),
        train_days=300,
        test_days=10,
        step_days=10,
        data_path=Path("/offline/data"),
        search_strategy="grid",
    )
    window = report["windows"][0]
    assert window["train_search"]["holdout"]["origin"] == "automatic"
    assert window["train_search"]["holdout"]["holdout_start"] == "2024-08-28"
    assert window["best_train_result"]["end_date"] == "2024-08-22"
    assert window["test_start"] == "2024-10-27"
    assert window["train_full_period_result"]["metrics"] == metrics(3)


@pytest.mark.parametrize("mode", ["grid", "successive_halving", "optuna"])
def test_all_training_failures_have_no_winner_or_diagnostic_execution(mode: str) -> None:
    report, runner = sweep(
        {"A": RuntimeError("train failed"), "B": RuntimeError("train failed")},
        {"A": metrics(9), "B": metrics(8)},
        mode=mode,
    )
    assert report["summary"]["best_result"] is None
    assert report["summary"]["holdout_evaluated_runs"] == 0
    assert report["summary"]["full_period_result"] is None
    assert all(c[1] == "2024-03-05" for c in runner.calls)


@pytest.mark.asyncio
async def test_incomplete_training_trial_is_persisted_as_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _ = sweep({"A": metrics(3), "B": metrics(1)})
    result = report["results"][1]
    result.update(
        completed_full_run=False,
        selection_eligible=False,
        objective_value=None,
        selection_reason="Full training evaluation did not complete",
    )
    session = Session()
    monkeypatch.setattr(worker, "async_session_factory", lambda: session)
    await worker._finalize_job("offline-job", report)
    assert session.trials[1].status == "incomplete"
    assert session.trials[1].objective_value is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "score", [None, float("nan"), float("inf"), {"sharpe_ratio": 1, "max_drawdown": None}]
)
async def test_unavailable_test_objective_does_not_break_training_finalization(
    monkeypatch: pytest.MonkeyPatch, score: Any
) -> None:
    # Diagnostic aggregation must tolerate the same unavailable values retained in JSON.
    report = walk_report(test_score=score)
    session = Session()
    monkeypatch.setattr(worker, "async_session_factory", lambda: session)
    await worker._finalize_job("offline-job", report)
    assert session.committed is True
    assert session.job.best_config == {"choice": "B"}
    assert session.job.best_metrics == metrics(3)
