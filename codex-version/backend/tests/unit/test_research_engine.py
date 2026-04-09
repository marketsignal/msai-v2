from __future__ import annotations

from datetime import date
from pathlib import Path

import msai.services.research_engine as research_engine_module
from msai.services.nautilus.backtest_runner import BacktestResult
from msai.services.research_engine import (
    ResearchEngine,
    build_walk_forward_windows,
    expand_parameter_grid,
    generalization_gap,
    rank_results,
    resolve_train_holdout_split,
    stability_ratio,
)


class StubRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(
        self,
        *,
        strategy_path: str,
        config: dict,
        instruments: list[str],
        start_date: str,
        end_date: str,
        data_path: Path,
        timeout_seconds: int,
    ) -> BacktestResult:
        self.calls.append(
            {
                "strategy_path": strategy_path,
                "config": dict(config),
                "instruments": list(instruments),
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        lookback = int(config["lookback"])
        sharpe = 5.0 if lookback == 20 else 1.0
        return BacktestResult(
            orders_df=_empty_frame(),
            positions_df=_empty_frame(),
            account_df=_empty_frame(),
            metrics={
                "sharpe": sharpe,
                "sortino": sharpe + 0.5,
                "max_drawdown": -0.1 * sharpe,
                "total_return": 0.02 * sharpe,
                "win_rate": 0.5,
                "num_trades": 10,
            },
        )


class InstrumentResolvingEngine(ResearchEngine):
    def _resolve_backtest_instruments(self, instruments: list[str], data_path: Path) -> list[str]:
        _ = data_path
        return [f"{instrument}.CANON" for instrument in instruments]


def test_expand_parameter_grid_returns_cartesian_product() -> None:
    grid = expand_parameter_grid({"lookback": [10, 20], "entry_zscore": [1.0, 1.5]})

    assert len(grid) == 4
    assert {"lookback": 10, "entry_zscore": 1.0} in grid
    assert {"lookback": 20, "entry_zscore": 1.5} in grid


def test_build_walk_forward_windows_rolls_forward() -> None:
    windows = build_walk_forward_windows(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
        train_days=30,
        test_days=10,
        step_days=10,
    )

    assert windows[0].train_start == date(2024, 1, 1)
    assert windows[0].test_start == date(2024, 1, 31)
    assert windows[1].train_start == date(2024, 1, 11)


def test_build_walk_forward_windows_supports_expanding_mode() -> None:
    windows = build_walk_forward_windows(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
        train_days=30,
        test_days=10,
        step_days=10,
        mode="expanding",
    )

    assert windows[0].train_start == date(2024, 1, 1)
    assert windows[1].train_start == date(2024, 1, 1)
    assert windows[1].train_end == date(2024, 2, 9)


def test_rank_results_keeps_successful_runs_ahead_of_errors() -> None:
    ranked = rank_results(
        [
            {"config": {"lookback": 10}, "metrics": {"sharpe": 0.5}, "error": None},
            {"config": {"lookback": 20}, "metrics": {"sharpe": 1.5}, "error": None},
            {"config": {"lookback": 30}, "metrics": None, "error": "boom"},
        ],
        objective="sharpe",
    )

    assert ranked[0]["config"]["lookback"] == 20
    assert ranked[-1]["error"] == "boom"


def test_run_parameter_sweep_ranks_best_config_first(tmp_path: Path) -> None:
    runner = StubRunner()
    engine = ResearchEngine(runner=runner, research_root=tmp_path)

    report = engine.run_parameter_sweep(
        strategy_path="strategies/example/mean_reversion.py",
        base_config={"trade_size": "1"},
        parameter_grid={"lookback": [10, 20]},
        instruments=["AAPL.XNAS"],
        start_date="2024-01-01",
        end_date="2024-01-31",
        data_path=tmp_path,
        objective="sharpe",
    )

    assert report["summary"]["successful_runs"] == 2
    assert report["summary"]["best_result"]["config"]["lookback"] == 20
    assert len(runner.calls) == 2
    assert report["search"]["strategy"] == "grid"


def test_run_parameter_sweep_successive_halving_prunes_weaker_candidates(tmp_path: Path) -> None:
    runner = StubRunner()
    engine = ResearchEngine(runner=runner, research_root=tmp_path)

    report = engine.run_parameter_sweep(
        strategy_path="strategies/example/mean_reversion.py",
        base_config={"trade_size": "1"},
        parameter_grid={"lookback": [10, 20]},
        instruments=["AAPL.XNAS"],
        start_date="2024-01-01",
        end_date="2024-06-30",
        data_path=tmp_path,
        objective="sharpe",
        search_strategy="successive_halving",
        stage_fractions=[0.5, 1.0],
        reduction_factor=2,
    )

    assert report["search"]["strategy"] == "successive_halving"
    assert report["summary"]["fully_evaluated_runs"] == 1
    assert report["summary"]["pruned_runs"] == 1
    assert report["summary"]["best_result"]["config"]["lookback"] == 20
    assert len(runner.calls) == 4
    pruned = next(result for result in report["results"] if result["config"]["lookback"] == 10)
    assert pruned["pruned"] is True
    assert pruned["completed_full_run"] is False
    assert len(pruned["stage_results"]) == 1


def test_resolve_train_holdout_split_builds_purged_holdout() -> None:
    split = resolve_train_holdout_split(
        start_date="2024-01-01",
        end_date="2024-12-31",
        holdout_fraction=0.2,
        holdout_days=None,
        purge_days=5,
    )

    assert split is not None
    assert split["holdout_start"] > split["train_end"]
    assert split["purge_days"] == 5


def test_run_walk_forward_uses_best_train_config_for_test_windows(tmp_path: Path) -> None:
    runner = StubRunner()
    engine = ResearchEngine(runner=runner, research_root=tmp_path)

    report = engine.run_walk_forward(
        strategy_path="strategies/example/mean_reversion.py",
        base_config={"trade_size": "1"},
        parameter_grid={"lookback": [10, 20]},
        instruments=["AAPL.XNAS"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
        train_days=30,
        test_days=10,
        step_days=20,
        data_path=tmp_path,
        objective="sharpe",
    )

    assert report["summary"]["successful_test_windows"] >= 1
    assert all(
        window["best_train_result"]["config"]["lookback"] == 20
        for window in report["windows"]
        if window["best_train_result"] is not None
    )
    assert report["summary"]["stability_ratio"] > 0
    assert report["summary"]["best_config_consistency"] == 1.0


def test_run_walk_forward_expanding_mode_marks_summary(tmp_path: Path) -> None:
    runner = StubRunner()
    engine = ResearchEngine(runner=runner, research_root=tmp_path)

    report = engine.run_walk_forward(
        strategy_path="strategies/example/mean_reversion.py",
        base_config={"trade_size": "1"},
        parameter_grid={"lookback": [10, 20]},
        instruments=["AAPL.EQUS"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
        train_days=30,
        test_days=10,
        step_days=10,
        mode="expanding",
        data_path=tmp_path,
        objective="sharpe",
    )

    assert report["walk_forward_mode"] == "expanding"
    assert report["summary"]["mode"] == "expanding"
    assert report["windows"][1]["train_start"] == "2024-01-01"


def test_run_parameter_sweep_uses_resolved_backtest_instruments(tmp_path: Path) -> None:
    runner = StubRunner()
    engine = InstrumentResolvingEngine(runner=runner, research_root=tmp_path)

    report = engine.run_parameter_sweep(
        strategy_path="strategies/example/mean_reversion.py",
        base_config={"trade_size": "1"},
        parameter_grid={"lookback": [10, 20]},
        instruments=["NQ.v.0"],
        start_date="2024-01-01",
        end_date="2024-01-31",
        data_path=tmp_path,
        objective="sharpe",
    )

    assert report["instruments"] == ["NQ.v.0.CANON"]
    assert all(call["instruments"][0].startswith("NQ.v.0.CANON") for call in runner.calls)


def test_run_walk_forward_uses_resolved_backtest_instruments(tmp_path: Path) -> None:
    runner = StubRunner()
    engine = InstrumentResolvingEngine(runner=runner, research_root=tmp_path)

    report = engine.run_walk_forward(
        strategy_path="strategies/example/mean_reversion.py",
        base_config={"trade_size": "1"},
        parameter_grid={"lookback": [10, 20]},
        instruments=["NQ.v.0"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 3, 31),
        train_days=30,
        test_days=10,
        step_days=20,
        data_path=tmp_path,
        objective="sharpe",
    )

    assert report["instruments"] == ["NQ.v.0.CANON"]
    assert all(call["instruments"][0].startswith("NQ.v.0.CANON") for call in runner.calls)


def test_run_parameter_sweep_optuna_returns_ranked_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _FakeTrial:
        def __init__(self, number: int) -> None:
            self.number = number
            self.user_attrs: dict[str, object] = {}

        def suggest_categorical(self, name: str, choices: list[object]) -> object:
            _ = name
            return choices[min(self.number, len(choices) - 1)]

        def set_user_attr(self, key: str, value: object) -> None:
            self.user_attrs[key] = value

    class _FakeState:
        name = "COMPLETE"

    class _FakeStudy:
        def __init__(self) -> None:
            self.counter = 0
            self.trials: list[object] = []

        def ask(self) -> _FakeTrial:
            trial = _FakeTrial(self.counter)
            self.counter += 1
            return trial

        def tell(self, trial: _FakeTrial, value=None, state=None) -> None:  # noqa: ANN001
            trial.value = value
            trial.state = state or _FakeState()
            self.trials.append(trial)

        def get_trials(self, deepcopy: bool = False) -> list[object]:
            _ = deepcopy
            return self.trials

    def _fake_execute(requests: list[dict[str, object]], *, max_parallelism: int | None) -> list[dict[str, object]]:
        _ = max_parallelism
        results = []
        for request in requests:
            lookback = int(dict(request["config"])["lookback"])
            sharpe = 2.0 if lookback == 20 else 0.5
            results.append(
                {
                    "config": dict(request["config"]),
                    "start_date": str(request["train_start"]),
                    "end_date": str(request["train_end"]),
                    "error": None,
                    "metrics": {"sharpe": sharpe - 0.2, "num_trades": 20, "total_return": sharpe / 10, "max_drawdown": -0.1},
                    "train_metrics": {"sharpe": sharpe, "num_trades": 20, "total_return": sharpe / 8, "max_drawdown": -0.1},
                    "holdout_metrics": {"sharpe": sharpe - 0.2, "num_trades": 10, "total_return": sharpe / 10, "max_drawdown": -0.1},
                    "holdout_error": None,
                    "pruned": False,
                    "prune_reason": None,
                    "pruned_after_stage": None,
                    "completed_full_run": True,
                    "selection_basis": "holdout",
                    "stage_results": [],
                }
            )
        return results

    monkeypatch.setattr(research_engine_module, "create_study", lambda **_: _FakeStudy())
    monkeypatch.setattr(research_engine_module, "execute_optuna_trial_requests", _fake_execute)
    monkeypatch.setattr(research_engine_module.settings, "optuna_enabled", True)
    monkeypatch.setattr(research_engine_module.settings, "optuna_max_trials", 2)

    report = ResearchEngine(research_root=tmp_path).run_parameter_sweep(
        strategy_path="strategies/example/mean_reversion.py",
        base_config={"trade_size": "1"},
        parameter_grid={"lookback": [10, 20]},
        instruments=["AAPL.XNAS"],
        start_date="2024-01-01",
        end_date="2024-12-31",
        data_path=tmp_path,
        objective="sharpe",
        search_strategy="optuna",
        study_key="optuna-test",
        instruments_prepared=True,
    )

    assert report["search"]["strategy"] == "optuna"
    assert report["search"]["study_name"] == "optuna-test"
    assert report["summary"]["best_result"]["config"]["lookback"] == 20


def test_generalization_gap_and_stability_ratio_use_average_metrics() -> None:
    in_sample = [{"metrics": {"sharpe": 4.0}, "error": None}]
    out_of_sample = [{"metrics": {"sharpe": 2.0}, "error": None}]

    assert generalization_gap(
        in_sample_results=in_sample,
        out_of_sample_results=out_of_sample,
        metric="sharpe",
    ) == 2.0
    assert stability_ratio(
        in_sample_results=in_sample,
        out_of_sample_results=out_of_sample,
        metric="sharpe",
    ) == 0.5


def _empty_frame():
    import pandas as pd

    return pd.DataFrame()
