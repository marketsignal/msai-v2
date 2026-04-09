from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from itertools import product
from math import ceil
from os import cpu_count
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from optuna import create_study
from optuna.samplers import TPESampler
from optuna.storages import JournalStorage
from optuna.storages.journal import JournalFileBackend
from optuna.trial import TrialState

from msai.core.config import settings
from msai.core.database import async_session_factory
from msai.services.nautilus.backtest_runner import BacktestRunner
from msai.services.nautilus.catalog_builder import ensure_catalog_data
from msai.services.nautilus.instrument_service import instrument_service
from msai.services.nautilus.strategy_config import prepare_backtest_strategy_config


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    train_start: date
    train_end: date
    test_start: date
    test_end: date


@dataclass(frozen=True, slots=True)
class BacktestRunSpec:
    strategy_path: str
    config: dict[str, Any]
    instruments: list[str]
    start_date: str
    end_date: str
    data_path: str


class ResearchEngine:
    def __init__(
        self,
        *,
        runner: BacktestRunner | None = None,
        research_root: Path | None = None,
    ) -> None:
        self.runner = runner or BacktestRunner()
        self.research_root = research_root or settings.research_root

    def _resolve_backtest_instruments(self, instruments: list[str], data_path: Path) -> list[str]:
        if type(self.runner) is not BacktestRunner:
            return list(instruments)

        async def _prepare() -> list[str]:
            async with async_session_factory() as session:
                definitions = await instrument_service.ensure_backtest_definitions(
                    session,
                    instruments,
                )
                await session.commit()
            return ensure_catalog_data(
                definitions=definitions,
                raw_parquet_root=settings.parquet_root,
                catalog_root=data_path,
            )

        return asyncio.run(_prepare())

    def run_parameter_sweep(
        self,
        *,
        strategy_path: str,
        base_config: dict[str, Any],
        parameter_grid: dict[str, list[Any]],
        instruments: list[str],
        start_date: str,
        end_date: str,
        data_path: Path,
        objective: str = "sharpe",
        max_parallelism: int | None = None,
        search_strategy: str = "auto",
        stage_fractions: list[float] | None = None,
        reduction_factor: int = 2,
        min_trades: int | None = None,
        require_positive_return: bool = False,
        holdout_fraction: float | None = None,
        holdout_days: int | None = None,
        purge_days: int = 5,
        study_key: str | None = None,
        instruments_prepared: bool = False,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        resolved_instruments = list(instruments) if instruments_prepared else self._resolve_backtest_instruments(instruments, data_path)
        candidate_count = count_parameter_grid(parameter_grid)
        split = resolve_train_holdout_split(
            start_date=start_date,
            end_date=end_date,
            holdout_fraction=holdout_fraction,
            holdout_days=holdout_days,
            purge_days=purge_days,
        )
        train_start = start_date
        train_end = split["train_end"] if split is not None else end_date
        strategy_used = resolve_search_strategy(
            requested_strategy=search_strategy,
            candidate_count=candidate_count,
            start_date=train_start,
            end_date=train_end,
        )
        resolved_min_trades = min_trades
        if resolved_min_trades is None and strategy_used in {"successive_halving", "regime_halving", "optuna"}:
            resolved_min_trades = 10
        if strategy_used == "optuna":
            return self._run_optuna_parameter_sweep(
                strategy_path=strategy_path,
                base_config=base_config,
                parameter_grid=parameter_grid,
                instruments=resolved_instruments,
                start_date=start_date,
                end_date=end_date,
                train_start=train_start,
                train_end=train_end,
                split=split,
                data_path=data_path,
                objective=objective,
                max_parallelism=max_parallelism,
                stage_fractions=stage_fractions,
                min_trades=resolved_min_trades,
                require_positive_return=require_positive_return,
                study_key=study_key,
                progress_callback=progress_callback,
            )
        combinations = expand_parameter_grid(parameter_grid)
        stage_summaries: list[dict[str, Any]] = []
        if strategy_used in {"successive_halving", "regime_halving"}:
            results, stage_summaries, survivors = self._run_successive_halving(
                strategy_path=strategy_path,
                base_config=base_config,
                parameter_combinations=combinations,
                instruments=resolved_instruments,
                start_date=train_start,
                end_date=train_end,
                data_path=data_path,
                objective=objective,
                max_parallelism=max_parallelism,
                stage_fractions=stage_fractions,
                reduction_factor=reduction_factor,
                min_trades=resolved_min_trades,
                require_positive_return=require_positive_return,
                screening_mode=strategy_used,
                progress_callback=progress_callback,
            )
        else:
            survivors = list(range(len(combinations)))
            if progress_callback is not None:
                progress_callback(
                    {
                        "progress": 10,
                        "message": f"Running {len(combinations)} training candidates",
                        "completed_trials": 0,
                        "total_trials": len(combinations),
                    }
                )
            results = [
                {
                    "config": to_jsonable({**base_config, **params}),
                    "start_date": train_start,
                    "end_date": train_end,
                    "error": None,
                    "metrics": None,
                    "pruned": False,
                    "prune_reason": None,
                    "pruned_after_stage": None,
                    "completed_full_run": False,
                    "stage_results": [],
                }
                for params in combinations
            ]

        if survivors:
            full_train_results = self._execute_stage_specs(
                candidate_indexes=survivors,
                candidates=results,
                strategy_path=strategy_path,
                instruments=resolved_instruments,
                stage_ranges=[{"start_date": train_start, "end_date": train_end, "label": "train_full"}],
                data_path=data_path,
                max_parallelism=max_parallelism,
            )
            for candidate_index, full_result in full_train_results:
                results[candidate_index]["train_metrics"] = full_result.get("metrics")
                results[candidate_index]["metrics"] = full_result.get("metrics")
                results[candidate_index]["error"] = full_result.get("error")
                results[candidate_index]["completed_full_run"] = full_result.get("error") is None
                results[candidate_index]["selection_basis"] = "train"
                results[candidate_index]["stage_results"].append(
                    {
                        "stage_index": len(stage_summaries) + 1,
                        "stage_count": len(stage_summaries) + (2 if split is not None else 1),
                        "fraction": 1.0,
                        "start_date": train_start,
                        "end_date": train_end,
                        "label": "train_full",
                        "error": full_result.get("error"),
                        "metrics": full_result.get("metrics"),
                    }
                )
            stage_summaries.append(
                {
                    "stage_index": len(stage_summaries) + 1,
                    "stage_count": len(stage_summaries) + (2 if split is not None else 1),
                    "label": "train_full",
                    "evaluated_runs": len(survivors),
                    "eligible_runs": sum(1 for index in survivors if results[index].get("error") is None),
                    "survivors_after_stage": sum(
                        1 for index in survivors if results[index].get("completed_full_run")
                    ),
                }
            )

        holdout_evaluated = 0
        if split is not None:
            holdout_candidates = [
                index
                for index in survivors
                if results[index].get("error") is None and bool(results[index].get("completed_full_run"))
            ]
            if holdout_candidates:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "progress": 90,
                            "message": f"Evaluating {len(holdout_candidates)} candidates on purged holdout",
                            "completed_trials": len(holdout_candidates),
                            "total_trials": len(results),
                        }
                    )
                holdout_results = self._execute_stage_specs(
                    candidate_indexes=holdout_candidates,
                    candidates=results,
                    strategy_path=strategy_path,
                    instruments=resolved_instruments,
                    stage_ranges=[
                        {
                            "start_date": split["holdout_start"],
                            "end_date": split["holdout_end"],
                            "label": "holdout",
                        }
                    ],
                    data_path=data_path,
                    max_parallelism=max_parallelism,
                )
                holdout_evaluated = len(holdout_results)
                for candidate_index, holdout_result in holdout_results:
                    results[candidate_index]["holdout_metrics"] = holdout_result.get("metrics")
                    results[candidate_index]["holdout_error"] = holdout_result.get("error")
                    results[candidate_index]["selection_basis"] = "holdout"
                    if holdout_result.get("error") is None:
                        results[candidate_index]["metrics"] = holdout_result.get("metrics")
                    results[candidate_index]["stage_results"].append(
                        {
                            "stage_index": len(stage_summaries) + 1,
                            "stage_count": len(stage_summaries) + 1,
                            "fraction": split["holdout_fraction"],
                            "start_date": split["holdout_start"],
                            "end_date": split["holdout_end"],
                            "label": "holdout",
                            "error": holdout_result.get("error"),
                            "metrics": holdout_result.get("metrics"),
                        }
                    )
                stage_summaries.append(
                    {
                        "stage_index": len(stage_summaries) + 1,
                        "stage_count": len(stage_summaries) + 1,
                        "label": "holdout",
                        "evaluated_runs": holdout_evaluated,
                        "eligible_runs": sum(
                            1 for index in holdout_candidates if results[index].get("holdout_error") is None
                        ),
                        "survivors_after_stage": sum(
                            1 for index in holdout_candidates if results[index].get("holdout_error") is None
                        ),
                        "purge_days": split["purge_days"],
                    }
                )

        ranked_results = rank_results(results, objective=objective)
        if progress_callback is not None:
            progress_callback(
                {
                    "progress": 98,
                    "message": "Finalizing research report",
                    "completed_trials": len(ranked_results),
                    "total_trials": len(ranked_results),
                }
            )
        best_result = next(
            (
                result
                for result in ranked_results
                if result.get("error") is None
                and result.get("holdout_error") is None
                and not bool(result.get("pruned"))
                and bool(result.get("completed_full_run"))
                and result.get("selection_basis") == "holdout"
            ),
            next(
                (
                    result
                    for result in ranked_results
                    if result.get("error") is None
                    and not bool(result.get("pruned"))
                    and bool(result.get("completed_full_run"))
                ),
                None,
            ),
        )
        full_period_result = None
        if split is not None and best_result is not None:
            full_period_result = self._run_one(
                strategy_path=strategy_path,
                config=dict(best_result["config"]),
                instruments=resolved_instruments,
                start_date=start_date,
                end_date=end_date,
                data_path=data_path,
            )
        return {
            "mode": "parameter_sweep",
            "generated_at": datetime.now(UTC).isoformat(),
            "objective": objective,
            "strategy_path": strategy_path,
            "base_config": to_jsonable(base_config),
            "parameter_grid": to_jsonable(parameter_grid),
            "instruments": resolved_instruments,
            "start_date": start_date,
            "end_date": end_date,
            "search": {
                "strategy": strategy_used,
                "stage_fractions": normalize_stage_fractions(stage_fractions),
                "reduction_factor": reduction_factor,
                "min_trades": resolved_min_trades,
                "require_positive_return": require_positive_return,
                "holdout": split,
            },
            "summary": {
                "total_runs": len(ranked_results),
                "successful_runs": sum(1 for result in ranked_results if result.get("error") is None),
                "fully_evaluated_runs": sum(1 for result in ranked_results if bool(result.get("completed_full_run"))),
                "pruned_runs": sum(1 for result in ranked_results if bool(result.get("pruned"))),
                "holdout_evaluated_runs": holdout_evaluated,
                "best_result": best_result,
                "full_period_result": full_period_result,
            },
            "stage_summaries": stage_summaries,
            "results": ranked_results,
        }

    def run_walk_forward(
        self,
        *,
        strategy_path: str,
        base_config: dict[str, Any],
        parameter_grid: dict[str, list[Any]],
        instruments: list[str],
        start_date: date,
        end_date: date,
        train_days: int,
        test_days: int,
        step_days: int | None,
        mode: str = "rolling",
        data_path: Path,
        objective: str = "sharpe",
        max_parallelism: int | None = None,
        search_strategy: str = "auto",
        stage_fractions: list[float] | None = None,
        reduction_factor: int = 2,
        min_trades: int | None = None,
        require_positive_return: bool = False,
        holdout_fraction: float | None = None,
        holdout_days: int | None = None,
        purge_days: int = 5,
        study_key: str | None = None,
        instruments_prepared: bool = False,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        resolved_instruments = list(instruments) if instruments_prepared else self._resolve_backtest_instruments(instruments, data_path)
        windows = build_walk_forward_windows(
            start_date=start_date,
            end_date=end_date,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            mode=mode,
        )

        payload_windows: list[dict[str, Any]] = []
        out_of_sample_results: list[dict[str, Any]] = []
        in_sample_results: list[dict[str, Any]] = []
        if type(self.runner) is not BacktestRunner:
            window_payloads = self._run_walk_forward_serial(
                strategy_path=strategy_path,
                base_config=base_config,
                parameter_grid=parameter_grid,
                instruments=resolved_instruments,
                windows=windows,
                data_path=data_path,
                objective=objective,
                search_strategy=search_strategy,
                stage_fractions=stage_fractions,
                reduction_factor=reduction_factor,
                min_trades=min_trades,
                require_positive_return=require_positive_return,
                holdout_fraction=holdout_fraction,
                holdout_days=holdout_days,
                purge_days=purge_days,
                study_key=study_key,
                progress_callback=progress_callback,
            )
        else:
            window_payloads = execute_walk_forward_windows(
                strategy_path=strategy_path,
                base_config=base_config,
                parameter_grid=parameter_grid,
                instruments=resolved_instruments,
                windows=windows,
                data_path=data_path,
                objective=objective,
                max_parallelism=max_parallelism,
                search_strategy=search_strategy,
                stage_fractions=stage_fractions,
                reduction_factor=reduction_factor,
                min_trades=min_trades,
                require_positive_return=require_positive_return,
                holdout_fraction=holdout_fraction,
                holdout_days=holdout_days,
                purge_days=purge_days,
                study_key=study_key,
            )
        for window_payload in window_payloads:
            best_train_result = window_payload.get("best_train_result")
            if best_train_result is not None:
                in_sample_results.append(best_train_result)
            test_result = window_payload.get("test_result")
            if test_result and test_result.get("error") is None:
                out_of_sample_results.append(test_result)
            payload_windows.append(window_payload)

        summary = {
            "mode": mode,
            "window_count": len(payload_windows),
            "successful_test_windows": len(out_of_sample_results),
            "avg_train_sharpe": average_metric(in_sample_results, "sharpe"),
            "avg_test_sharpe": average_metric(out_of_sample_results, "sharpe"),
            "avg_test_total_return": average_metric(out_of_sample_results, "total_return"),
            "avg_test_win_rate": average_metric(out_of_sample_results, "win_rate"),
            "worst_test_drawdown": min_metric(out_of_sample_results, "max_drawdown"),
            "generalization_gap": generalization_gap(
                in_sample_results=in_sample_results,
                out_of_sample_results=out_of_sample_results,
                metric="sharpe",
            ),
            "stability_ratio": stability_ratio(
                in_sample_results=in_sample_results,
                out_of_sample_results=out_of_sample_results,
                metric="sharpe",
            ),
            "best_config_consistency": best_config_consistency(payload_windows),
        }

        return {
            # The nested train-window sweeps resolve their own auto defaults, but we expose
            # the same default min-trades policy here for operator clarity.
            "mode": "walk_forward",
            "generated_at": datetime.now(UTC).isoformat(),
            "objective": objective,
            "strategy_path": strategy_path,
            "base_config": to_jsonable(base_config),
            "parameter_grid": to_jsonable(parameter_grid),
            "search": {
                "strategy": resolve_search_strategy(
                    requested_strategy=search_strategy,
                    candidate_count=len(expand_parameter_grid(parameter_grid)),
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                ),
                "stage_fractions": normalize_stage_fractions(stage_fractions),
                "reduction_factor": reduction_factor,
                "min_trades": min_trades,
                "require_positive_return": require_positive_return,
                "holdout_fraction": holdout_fraction,
                "holdout_days": holdout_days,
                "purge_days": purge_days,
            },
            "instruments": resolved_instruments,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "train_days": train_days,
            "test_days": test_days,
            "step_days": step_days or test_days,
            "walk_forward_mode": mode,
            "summary": summary,
            "windows": payload_windows,
        }

    def _run_walk_forward_serial(
        self,
        *,
        strategy_path: str,
        base_config: dict[str, Any],
        parameter_grid: dict[str, list[Any]],
        instruments: list[str],
        windows: list[WalkForwardWindow],
        data_path: Path,
        objective: str,
        search_strategy: str,
        stage_fractions: list[float] | None,
        reduction_factor: int,
        min_trades: int | None,
        require_positive_return: bool,
        holdout_fraction: float | None,
        holdout_days: int | None,
        purge_days: int,
        study_key: str | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> list[dict[str, Any]]:
        payload_windows: list[dict[str, Any]] = []
        total_windows = len(windows)
        for index, window in enumerate(windows, start=1):
            if progress_callback is not None:
                progress_callback(
                    {
                        "progress": 10 + int(80 * ((index - 1) / max(1, total_windows))),
                        "message": f"Running walk-forward window {index} of {total_windows}",
                        "stage_index": index,
                        "stage_count": total_windows,
                        "completed_trials": index - 1,
                        "total_trials": total_windows,
                    }
                )
            train_report = self.run_parameter_sweep(
                strategy_path=strategy_path,
                base_config=base_config,
                parameter_grid=parameter_grid,
                instruments=instruments,
                start_date=window.train_start.isoformat(),
                end_date=window.train_end.isoformat(),
                data_path=data_path,
                objective=objective,
                max_parallelism=1,
                search_strategy=search_strategy,
                stage_fractions=stage_fractions,
                reduction_factor=reduction_factor,
                min_trades=min_trades,
                require_positive_return=require_positive_return,
                holdout_fraction=holdout_fraction,
                holdout_days=holdout_days,
                purge_days=purge_days,
                study_key=window_study_key(study_key, index),
            )
            best_train_result = train_report["summary"]["best_result"]
            window_payload = {
                "train_start": window.train_start.isoformat(),
                "train_end": window.train_end.isoformat(),
                "test_start": window.test_start.isoformat(),
                "test_end": window.test_end.isoformat(),
                "train_results": train_report["results"],
                "best_train_result": best_train_result,
                "test_result": None,
            }
            if best_train_result is not None:
                window_payload["test_result"] = self._run_one(
                    strategy_path=strategy_path,
                    config={**base_config, **dict(best_train_result["config"])},
                    instruments=instruments,
                    start_date=window.test_start.isoformat(),
                    end_date=window.test_end.isoformat(),
                    data_path=data_path,
                )
            payload_windows.append(window_payload)
        if progress_callback is not None:
            progress_callback(
                {
                    "progress": 95,
                    "message": "Finalizing walk-forward report",
                    "stage_index": total_windows,
                    "stage_count": total_windows,
                    "completed_trials": total_windows,
                    "total_trials": total_windows,
                }
            )
        return payload_windows

    def _run_optuna_parameter_sweep(
        self,
        *,
        strategy_path: str,
        base_config: dict[str, Any],
        parameter_grid: dict[str, list[Any]],
        instruments: list[str],
        start_date: str,
        end_date: str,
        train_start: str,
        train_end: str,
        split: dict[str, Any] | None,
        data_path: Path,
        objective: str,
        max_parallelism: int | None,
        stage_fractions: list[float] | None,
        min_trades: int | None,
        require_positive_return: bool,
        study_key: str | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        if not settings.optuna_enabled:
            raise ValueError("Optuna search is disabled by configuration")

        study_name = resolve_optuna_study_name(
            study_key=study_key,
            strategy_path=strategy_path,
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            objective=objective,
        )
        settings.optuna_root.mkdir(parents=True, exist_ok=True)
        storage_path = settings.optuna_root / f"{sanitize_study_name(study_name)}.journal"
        study = create_study(
            study_name=study_name,
            direction="maximize",
            sampler=TPESampler(),
            storage=JournalStorage(JournalFileBackend(str(storage_path))),
            load_if_exists=True,
        )
        stages = build_screening_stages(
            screening_mode="regime_halving",
            instruments=instruments,
            start_date=train_start,
            end_date=train_end,
            stage_fractions=stage_fractions,
        )
        grid_limit = count_parameter_grid(parameter_grid)
        target_trials = min(int(settings.optuna_max_trials), grid_limit)
        parallelism = _resolved_parallelism(
            run_specs_count=max(1, min(target_trials, max_parallelism or settings.research_max_parallelism)),
            max_parallelism=max_parallelism,
        )
        history = load_optuna_trial_history(study)
        results: list[dict[str, Any]] = []
        terminal_trials = sum(1 for state in history.values() if state["state"] in {"complete", "pruned", "fail"})

        while terminal_trials < target_trials and len(history) < grid_limit:
            batch_trials: list[tuple[object, str, dict[str, Any]]] = []
            attempts = 0
            max_attempts = max(target_trials * 4, parallelism * 8)
            while len(batch_trials) < max(1, parallelism) and attempts < max_attempts and len(history) < grid_limit:
                trial = study.ask()
                params = {
                    key: trial.suggest_categorical(key, list(values))
                    for key, values in sorted(parameter_grid.items())
                }
                candidate_config = {**base_config, **params}
                config_key = config_cache_key(candidate_config)
                trial.set_user_attr("config_key", config_key)
                cached = history.get(config_key)
                if cached is not None:
                    if cached["state"] == "complete" and cached["value"] is not None:
                        study.tell(trial, float(cached["value"]))
                    elif cached["state"] == "pruned":
                        study.tell(trial, state=TrialState.PRUNED)
                    else:
                        study.tell(trial, state=TrialState.FAIL)
                    attempts += 1
                    continue
                history[config_key] = {"state": "pending", "value": None}
                batch_trials.append((trial, config_key, candidate_config))
                attempts += 1

            if not batch_trials:
                break

            if progress_callback is not None:
                progress_callback(
                    {
                        "progress": 10 + int(70 * (terminal_trials / max(1, target_trials))),
                        "message": f"Optuna evaluating batch of {len(batch_trials)} sampled candidates",
                        "completed_trials": terminal_trials,
                        "total_trials": target_trials,
                    }
                )

            batch_results = execute_optuna_trial_requests(
                [
                    {
                        "strategy_path": strategy_path,
                        "config": config,
                        "instruments": instruments,
                        "train_start": train_start,
                        "train_end": train_end,
                        "data_path": str(data_path),
                        "stages": stages,
                        "objective": objective,
                        "min_trades": min_trades,
                        "require_positive_return": require_positive_return,
                        "split": split,
                    }
                    for _, _, config in batch_trials
                ],
                max_parallelism=parallelism,
            )

            for (trial, config_key, _), candidate in zip(batch_trials, batch_results, strict=True):
                state = "fail"
                objective_value = None
                if candidate.get("pruned"):
                    study.tell(trial, state=TrialState.PRUNED)
                    state = "pruned"
                elif candidate.get("error") is not None or candidate.get("holdout_error") is not None:
                    study.tell(trial, state=TrialState.FAIL)
                else:
                    objective_value = objective_metric_value(candidate.get("metrics") or {}, objective)
                    study.tell(trial, objective_value)
                    state = "complete"
                history[config_key] = {"state": state, "value": objective_value}
                results.append(candidate)
                terminal_trials += 1

            if terminal_trials >= target_trials:
                break

        ranked_results = rank_results(results, objective=objective)
        best_result = next(
            (
                result
                for result in ranked_results
                if result.get("error") is None
                and result.get("holdout_error") is None
                and not bool(result.get("pruned"))
                and bool(result.get("completed_full_run"))
                and result.get("selection_basis") == "holdout"
            ),
            next(
                (
                    result
                    for result in ranked_results
                    if result.get("error") is None
                    and not bool(result.get("pruned"))
                    and bool(result.get("completed_full_run"))
                ),
                None,
            ),
        )
        full_period_result = None
        if split is not None and best_result is not None:
            full_period_result = self._run_one(
                strategy_path=strategy_path,
                config=dict(best_result["config"]),
                instruments=instruments,
                start_date=start_date,
                end_date=end_date,
                data_path=data_path,
            )
        if progress_callback is not None:
            progress_callback(
                {
                    "progress": 98,
                    "message": "Finalizing Optuna research report",
                    "completed_trials": len(results),
                    "total_trials": target_trials,
                }
            )
        stage_summaries = summarize_optuna_stages(results, stages=stages, split=split)
        return {
            "mode": "parameter_sweep",
            "generated_at": datetime.now(UTC).isoformat(),
            "objective": objective,
            "strategy_path": strategy_path,
            "base_config": to_jsonable(base_config),
            "parameter_grid": to_jsonable(parameter_grid),
            "instruments": instruments,
            "start_date": start_date,
            "end_date": end_date,
            "search": {
                "strategy": "optuna",
                "stage_fractions": normalize_stage_fractions(stage_fractions),
                "min_trades": min_trades,
                "require_positive_return": require_positive_return,
                "holdout": split,
                "study_name": study_name,
                "storage_path": str(storage_path),
                "target_trials": target_trials,
                "evaluated_trials": len(results),
                "parallelism": parallelism,
            },
            "summary": {
                "total_runs": len(ranked_results),
                "successful_runs": sum(1 for result in ranked_results if result.get("error") is None),
                "fully_evaluated_runs": sum(1 for result in ranked_results if bool(result.get("completed_full_run"))),
                "pruned_runs": sum(1 for result in ranked_results if bool(result.get("pruned"))),
                "holdout_evaluated_runs": sum(1 for result in ranked_results if result.get("holdout_metrics") is not None),
                "best_result": best_result,
                "full_period_result": full_period_result,
            },
            "stage_summaries": stage_summaries,
            "results": ranked_results,
        }

    def save_report(self, report: dict[str, Any], output_path: Path | None = None) -> Path:
        target = output_path or self.research_root / f"{uuid4()}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(to_jsonable(report), indent=2, sort_keys=True))
        return target

    def _run_one(
        self,
        *,
        strategy_path: str,
        config: dict[str, Any],
        instruments: list[str],
        start_date: str,
        end_date: str,
        data_path: Path,
    ) -> dict[str, Any]:
        resolved_config = prepare_backtest_strategy_config(config, instruments)
        try:
            result = self.runner.run(
                strategy_path=strategy_path,
                config=resolved_config,
                instruments=instruments,
                start_date=start_date,
                end_date=end_date,
                data_path=data_path,
                timeout_seconds=settings.backtest_timeout_seconds,
            )
        except Exception as exc:
            return {
                "config": to_jsonable(config),
                "start_date": start_date,
                "end_date": end_date,
                "error": str(exc),
                "metrics": None,
            }

        return {
            "config": to_jsonable(config),
            "start_date": start_date,
            "end_date": end_date,
            "error": None,
            "metrics": to_jsonable(result.metrics),
        }

    def _run_successive_halving(
        self,
        *,
        strategy_path: str,
        base_config: dict[str, Any],
        parameter_combinations: list[dict[str, Any]],
        instruments: list[str],
        start_date: str,
        end_date: str,
        data_path: Path,
        objective: str,
        max_parallelism: int | None,
        stage_fractions: list[float] | None,
        reduction_factor: int,
        min_trades: int | None,
        require_positive_return: bool,
        screening_mode: str,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int]]:
        if not parameter_combinations:
            return [], [], []

        stages = build_screening_stages(
            screening_mode=screening_mode,
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            stage_fractions=stage_fractions,
        )
        candidates = [
            {
                "config": to_jsonable({**base_config, **params}),
                "start_date": start_date,
                "end_date": end_date,
                "error": None,
                "metrics": None,
                "pruned": False,
                "prune_reason": None,
                "pruned_after_stage": None,
                "completed_full_run": False,
                "stage_results": [],
            }
            for params in parameter_combinations
        ]
        survivors = list(range(len(candidates)))
        stage_summaries: list[dict[str, Any]] = []
        total_candidates = len(candidates)

        for stage_index, stage in enumerate(stages, start=1):
            if not survivors:
                break
            if progress_callback is not None:
                progress_callback(
                    {
                        "progress": stage_progress(stage_index=stage_index, stage_count=len(stages), start=10, end=90),
                        "message": (
                            f"Stage {stage_index}/{len(stages)}: "
                            f"evaluating {len(survivors)} candidates on {stage['label']}"
                        ),
                        "stage_index": stage_index,
                        "stage_count": len(stages),
                        "completed_trials": total_candidates - len(survivors),
                        "total_trials": total_candidates,
                    }
                )

            stage_results = self._execute_stage_specs(
                candidate_indexes=survivors,
                candidates=candidates,
                strategy_path=strategy_path,
                instruments=instruments,
                stage_ranges=stage["ranges"],
                data_path=data_path,
                max_parallelism=max_parallelism,
            )
            eligible_indexes: list[int] = []
            eligible_results: list[dict[str, Any]] = []

            for candidate_index, result in stage_results:
                stage_payload = {
                    "stage_index": stage_index,
                    "stage_count": len(stages),
                    "fraction": stage["fraction"],
                    "label": stage["label"],
                    "ranges": to_jsonable(stage["ranges"]),
                    "error": result.get("error"),
                    "metrics": result.get("metrics"),
                }
                candidates[candidate_index]["stage_results"].append(to_jsonable(stage_payload))
                candidates[candidate_index]["metrics"] = result.get("metrics")
                candidates[candidate_index]["error"] = result.get("error")
                if is_stage_eligible(
                    result=result,
                    stage_fraction=stage["fraction"],
                    min_trades=min_trades,
                    require_positive_return=require_positive_return,
                ):
                    eligible_indexes.append(candidate_index)
                    eligible_results.append(with_search_metadata(result))
                elif result.get("error") is None:
                    mark_candidate_pruned(
                        candidate=candidates[candidate_index],
                        stage_index=stage_index,
                        reason=build_prune_reason(
                            result=result,
                            stage_fraction=stage["fraction"],
                            min_trades=min_trades,
                            require_positive_return=require_positive_return,
                        ),
                    )

            ranked_stage_pairs = rank_results(
                [
                    {"candidate_index": candidate_index, **result}
                    for candidate_index, result in zip(eligible_indexes, eligible_results, strict=True)
                ],
                objective=objective,
            )
            ranked_stage_indexes = [
                int(result["candidate_index"])
                for result in ranked_stage_pairs
            ]
            if stage_index == len(stages):
                survivors = ranked_stage_indexes
            else:
                keep_count = min(
                    len(ranked_stage_indexes),
                    max(1, ceil(len(ranked_stage_indexes) / reduction_factor)),
                )
                next_survivors = ranked_stage_indexes[:keep_count]
                pruned_indexes = [index for index in ranked_stage_indexes[keep_count:] if index not in next_survivors]
                for candidate_index in pruned_indexes:
                    mark_candidate_pruned(
                        candidate=candidates[candidate_index],
                        stage_index=stage_index,
                        reason=f"Pruned by successive halving after stage {stage_index}",
                    )
                survivors = next_survivors

            stage_summaries.append(
                {
                    "stage_index": stage_index,
                    "stage_count": len(stages),
                    "fraction": stage["fraction"],
                    "label": stage["label"],
                    "ranges": to_jsonable(stage["ranges"]),
                    "evaluated_runs": len(stage_results),
                    "eligible_runs": len(ranked_stage_indexes),
                    "survivors_after_stage": len(survivors),
                }
            )

        return [to_jsonable(candidate) for candidate in candidates], stage_summaries, survivors

    def _execute_stage_specs(
        self,
        *,
        candidate_indexes: list[int],
        candidates: list[dict[str, Any]],
        strategy_path: str,
        instruments: list[str],
        stage_ranges: list[dict[str, str]],
        data_path: Path,
        max_parallelism: int | None,
    ) -> list[tuple[int, dict[str, Any]]]:
        if len(stage_ranges) == 1:
            return self._execute_single_range_specs(
                candidate_indexes=candidate_indexes,
                candidates=candidates,
                strategy_path=strategy_path,
                instruments=instruments,
                start_date=str(stage_ranges[0]["start_date"]),
                end_date=str(stage_ranges[0]["end_date"]),
                data_path=data_path,
                max_parallelism=max_parallelism,
            )

        return [
            (
                candidate_index,
                aggregate_stage_run_results(
                    [
                        self._run_one(
                            strategy_path=strategy_path,
                            config=dict(candidates[candidate_index]["config"]),
                            instruments=instruments,
                            start_date=str(stage_range["start_date"]),
                            end_date=str(stage_range["end_date"]),
                            data_path=data_path,
                        )
                        for stage_range in stage_ranges
                    ]
                ),
            )
            for candidate_index in candidate_indexes
        ]

    def _execute_single_range_specs(
        self,
        *,
        candidate_indexes: list[int],
        candidates: list[dict[str, Any]],
        strategy_path: str,
        instruments: list[str],
        start_date: str,
        end_date: str,
        data_path: Path,
        max_parallelism: int | None,
    ) -> list[tuple[int, dict[str, Any]]]:
        if type(self.runner) is not BacktestRunner:
            return [
                (
                    candidate_index,
                    self._run_one(
                        strategy_path=strategy_path,
                        config=dict(candidates[candidate_index]["config"]),
                        instruments=instruments,
                        start_date=start_date,
                        end_date=end_date,
                        data_path=data_path,
                    ),
                )
                for candidate_index in candidate_indexes
            ]

        run_specs = [
            BacktestRunSpec(
                strategy_path=strategy_path,
                config=dict(candidates[candidate_index]["config"]),
                instruments=instruments,
                start_date=start_date,
                end_date=end_date,
                data_path=str(data_path),
            )
            for candidate_index in candidate_indexes
        ]
        results = execute_backtest_specs(run_specs, max_parallelism=max_parallelism)
        return list(zip(candidate_indexes, results, strict=True))


def execute_backtest_specs(
    run_specs: list[BacktestRunSpec],
    *,
    max_parallelism: int | None,
) -> list[dict[str, Any]]:
    if not run_specs:
        return []

    worker_count = _resolved_parallelism(run_specs_count=len(run_specs), max_parallelism=max_parallelism)
    if worker_count <= 1:
        return [_run_backtest_spec(run_spec) for run_spec in run_specs]

    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(_run_backtest_spec, run_specs))


def execute_optuna_trial_requests(
    requests: list[dict[str, Any]],
    *,
    max_parallelism: int | None,
) -> list[dict[str, Any]]:
    if not requests:
        return []

    worker_count = _resolved_parallelism(run_specs_count=len(requests), max_parallelism=max_parallelism)
    if worker_count <= 1:
        return [_run_optuna_trial_request(request) for request in requests]

    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(_run_optuna_trial_request, requests))


def execute_walk_forward_windows(
    *,
    strategy_path: str,
    base_config: dict[str, Any],
    parameter_grid: dict[str, list[Any]],
    instruments: list[str],
    windows: list[WalkForwardWindow],
    data_path: Path,
    objective: str,
    max_parallelism: int | None,
    search_strategy: str,
    stage_fractions: list[float] | None,
    reduction_factor: int,
    min_trades: int | None,
    require_positive_return: bool,
    holdout_fraction: float | None,
    holdout_days: int | None,
    purge_days: int,
    study_key: str | None,
) -> list[dict[str, Any]]:
    if not windows:
        return []

    requests = [
        {
            "strategy_path": strategy_path,
            "base_config": base_config,
            "parameter_grid": parameter_grid,
            "instruments": instruments,
            "window": {
                "train_start": window.train_start.isoformat(),
                "train_end": window.train_end.isoformat(),
                "test_start": window.test_start.isoformat(),
                "test_end": window.test_end.isoformat(),
            },
            "data_path": str(data_path),
            "objective": objective,
            "search_strategy": search_strategy,
            "stage_fractions": list(stage_fractions) if stage_fractions else None,
            "reduction_factor": reduction_factor,
            "min_trades": min_trades,
            "require_positive_return": require_positive_return,
            "holdout_fraction": holdout_fraction,
            "holdout_days": holdout_days,
            "purge_days": purge_days,
            "study_key": window_study_key(study_key, index + 1),
            "instruments_prepared": True,
        }
        for index, window in enumerate(windows)
    ]
    worker_count = _resolved_parallelism(run_specs_count=len(requests), max_parallelism=max_parallelism)
    if worker_count <= 1:
        return [_run_walk_forward_window(request) for request in requests]

    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(_run_walk_forward_window, requests))


def _run_walk_forward_window(request: dict[str, Any]) -> dict[str, Any]:
    train_report = ResearchEngine().run_parameter_sweep(
        strategy_path=str(request["strategy_path"]),
        base_config=dict(request["base_config"]),
        parameter_grid=dict(request["parameter_grid"]),
        instruments=list(request["instruments"]),
        start_date=str(request["window"]["train_start"]),
        end_date=str(request["window"]["train_end"]),
        data_path=Path(str(request["data_path"])),
        objective=str(request["objective"]),
        max_parallelism=1,
        search_strategy=str(request.get("search_strategy") or "auto"),
        stage_fractions=list(request["stage_fractions"]) if request.get("stage_fractions") else None,
        reduction_factor=int(request.get("reduction_factor") or 2),
        min_trades=int(request["min_trades"]) if request.get("min_trades") is not None else None,
        require_positive_return=bool(request.get("require_positive_return") or False),
        holdout_fraction=float(request["holdout_fraction"]) if request.get("holdout_fraction") is not None else None,
        holdout_days=int(request["holdout_days"]) if request.get("holdout_days") is not None else None,
        purge_days=int(request.get("purge_days") or 0),
        study_key=str(request.get("study_key")) if request.get("study_key") else None,
        instruments_prepared=bool(request.get("instruments_prepared")),
    )
    best_train_result = train_report["summary"]["best_result"]
    window_payload = {
        "train_start": request["window"]["train_start"],
        "train_end": request["window"]["train_end"],
        "test_start": request["window"]["test_start"],
        "test_end": request["window"]["test_end"],
        "train_results": train_report["results"],
        "best_train_result": best_train_result,
        "test_result": None,
    }
    if best_train_result is None:
        return window_payload

    test_result = _run_backtest_spec(
        BacktestRunSpec(
            strategy_path=str(request["strategy_path"]),
            config={**dict(request["base_config"]), **dict(best_train_result["config"])},
            instruments=list(request["instruments"]),
            start_date=str(request["window"]["test_start"]),
            end_date=str(request["window"]["test_end"]),
            data_path=str(request["data_path"]),
        )
    )
    window_payload["test_result"] = test_result
    return window_payload


def _run_backtest_spec(run_spec: BacktestRunSpec) -> dict[str, Any]:
    resolved_config = prepare_backtest_strategy_config(run_spec.config, run_spec.instruments)
    try:
        result = BacktestRunner().run(
            strategy_path=run_spec.strategy_path,
            config=resolved_config,
            instruments=run_spec.instruments,
            start_date=run_spec.start_date,
            end_date=run_spec.end_date,
            data_path=Path(run_spec.data_path),
            timeout_seconds=settings.backtest_timeout_seconds,
        )
    except Exception as exc:
        return {
            "config": to_jsonable(run_spec.config),
            "start_date": run_spec.start_date,
            "end_date": run_spec.end_date,
            "error": str(exc),
            "metrics": None,
        }

    return {
        "config": to_jsonable(run_spec.config),
        "start_date": run_spec.start_date,
        "end_date": run_spec.end_date,
        "error": None,
        "metrics": to_jsonable(result.metrics),
    }


def _run_optuna_trial_request(request: dict[str, Any]) -> dict[str, Any]:
    config = dict(request["config"])
    candidate = {
        "config": to_jsonable(config),
        "start_date": str(request["train_start"]),
        "end_date": str(request["train_end"]),
        "error": None,
        "metrics": None,
        "pruned": False,
        "prune_reason": None,
        "pruned_after_stage": None,
        "completed_full_run": False,
        "selection_basis": "train",
        "stage_results": [],
    }
    stages = list(request.get("stages") or [])
    split = request.get("split")
    final_stage_count = len(stages) + (2 if split is not None else 1)

    for stage_index, stage in enumerate(stages, start=1):
        stage_result = _execute_stage_ranges_for_config(
            strategy_path=str(request["strategy_path"]),
            config=config,
            instruments=list(request["instruments"]),
            stage_ranges=list(stage["ranges"]),
            data_path=Path(str(request["data_path"])),
        )
        candidate["stage_results"].append(
            {
                "stage_index": stage_index,
                "stage_count": final_stage_count,
                "fraction": stage["fraction"],
                "label": stage["label"],
                "ranges": to_jsonable(stage["ranges"]),
                "error": stage_result.get("error"),
                "metrics": stage_result.get("metrics"),
            }
        )
        candidate["metrics"] = stage_result.get("metrics")
        candidate["error"] = stage_result.get("error")
        if not is_stage_eligible(
            result=stage_result,
            stage_fraction=float(stage["fraction"]),
            min_trades=int(request["min_trades"]) if request.get("min_trades") is not None else None,
            require_positive_return=bool(request.get("require_positive_return") or False),
        ):
            if stage_result.get("error") is None:
                mark_candidate_pruned(
                    candidate,
                    stage_index=stage_index,
                    reason=build_prune_reason(
                        result=stage_result,
                        stage_fraction=float(stage["fraction"]),
                        min_trades=int(request["min_trades"]) if request.get("min_trades") is not None else None,
                        require_positive_return=bool(request.get("require_positive_return") or False),
                    ),
                )
            return candidate

    train_result = _run_backtest_spec(
        BacktestRunSpec(
            strategy_path=str(request["strategy_path"]),
            config=config,
            instruments=list(request["instruments"]),
            start_date=str(request["train_start"]),
            end_date=str(request["train_end"]),
            data_path=str(request["data_path"]),
        )
    )
    candidate["train_metrics"] = train_result.get("metrics")
    candidate["metrics"] = train_result.get("metrics")
    candidate["error"] = train_result.get("error")
    candidate["completed_full_run"] = train_result.get("error") is None
    candidate["stage_results"].append(
        {
            "stage_index": len(stages) + 1,
            "stage_count": final_stage_count,
            "fraction": 1.0,
            "start_date": str(request["train_start"]),
            "end_date": str(request["train_end"]),
            "label": "train_full",
            "error": train_result.get("error"),
            "metrics": train_result.get("metrics"),
        }
    )
    if train_result.get("error") is not None:
        return candidate

    if split is None:
        return candidate

    holdout_result = _run_backtest_spec(
        BacktestRunSpec(
            strategy_path=str(request["strategy_path"]),
            config=config,
            instruments=list(request["instruments"]),
            start_date=str(split["holdout_start"]),
            end_date=str(split["holdout_end"]),
            data_path=str(request["data_path"]),
        )
    )
    candidate["holdout_metrics"] = holdout_result.get("metrics")
    candidate["holdout_error"] = holdout_result.get("error")
    candidate["selection_basis"] = "holdout"
    if holdout_result.get("error") is None:
        candidate["metrics"] = holdout_result.get("metrics")
    candidate["stage_results"].append(
        {
            "stage_index": len(stages) + 2,
            "stage_count": final_stage_count,
            "fraction": split["holdout_fraction"],
            "start_date": str(split["holdout_start"]),
            "end_date": str(split["holdout_end"]),
            "label": "holdout",
            "error": holdout_result.get("error"),
            "metrics": holdout_result.get("metrics"),
        }
    )
    return candidate


def _execute_stage_ranges_for_config(
    *,
    strategy_path: str,
    config: dict[str, Any],
    instruments: list[str],
    stage_ranges: list[dict[str, str]],
    data_path: Path,
) -> dict[str, Any]:
    if len(stage_ranges) == 1:
        stage_range = stage_ranges[0]
        return _run_backtest_spec(
            BacktestRunSpec(
                strategy_path=strategy_path,
                config=config,
                instruments=instruments,
                start_date=str(stage_range["start_date"]),
                end_date=str(stage_range["end_date"]),
                data_path=str(data_path),
            )
        )
    return aggregate_stage_run_results(
        [
            _run_backtest_spec(
                BacktestRunSpec(
                    strategy_path=strategy_path,
                    config=config,
                    instruments=instruments,
                    start_date=str(stage_range["start_date"]),
                    end_date=str(stage_range["end_date"]),
                    data_path=str(data_path),
                )
            )
            for stage_range in stage_ranges
        ]
    )


def _resolved_parallelism(*, run_specs_count: int, max_parallelism: int | None) -> int:
    if run_specs_count < 2:
        return 1
    limit = max_parallelism or settings.research_max_parallelism
    return max(1, min(run_specs_count, limit, cpu_count() or 1))


def resolve_search_strategy(
    *,
    requested_strategy: str,
    candidate_count: int,
    start_date: str,
    end_date: str,
) -> str:
    if requested_strategy != "auto":
        return requested_strategy

    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        total_days = max(1, (end - start).days + 1)
    except ValueError:
        total_days = 1

    if candidate_count >= 8 and total_days >= 60:
        return "regime_halving"
    return "grid"


def normalize_stage_fractions(stage_fractions: list[float] | None) -> list[float]:
    fractions = list(stage_fractions or [0.35, 0.7, 1.0])
    cleaned = sorted({min(1.0, max(0.05, float(value))) for value in fractions})
    if not cleaned or cleaned[-1] < 1.0:
        cleaned.append(1.0)
    return cleaned


def build_screening_stages(
    *,
    screening_mode: str,
    instruments: list[str],
    start_date: str,
    end_date: str,
    stage_fractions: list[float] | None,
) -> list[dict[str, Any]]:
    if screening_mode == "regime_halving":
        regime_stages = build_regime_halving_stages(
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            stage_fractions=stage_fractions,
        )
        if regime_stages:
            return regime_stages
    return build_successive_halving_stages(
        start_date=start_date,
        end_date=end_date,
        stage_fractions=stage_fractions,
    )


def summarize_optuna_stages(
    results: list[dict[str, Any]],
    *,
    stages: list[dict[str, Any]],
    split: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    total_stage_count = len(stages) + (2 if split is not None else 1)

    for stage_index, stage in enumerate(stages, start=1):
        evaluated_runs = 0
        eligible_runs = 0
        for result in results:
            stage_result = next(
                (
                    item
                    for item in result.get("stage_results", [])
                    if int(item.get("stage_index") or 0) == stage_index
                ),
                None,
            )
            if stage_result is None:
                continue
            evaluated_runs += 1
            if stage_result.get("error") is None and (
                result.get("pruned_after_stage") is None
                or int(result.get("pruned_after_stage") or 0) > stage_index
            ):
                eligible_runs += 1
        summaries.append(
            {
                "stage_index": stage_index,
                "stage_count": total_stage_count,
                "fraction": stage["fraction"],
                "label": stage["label"],
                "ranges": to_jsonable(stage["ranges"]),
                "evaluated_runs": evaluated_runs,
                "eligible_runs": eligible_runs,
                "survivors_after_stage": eligible_runs,
            }
        )

    train_index = len(stages) + 1
    train_results = [
        result
        for result in results
        if any(int(item.get("stage_index") or 0) == train_index for item in result.get("stage_results", []))
    ]
    summaries.append(
        {
            "stage_index": train_index,
            "stage_count": total_stage_count,
            "label": "train_full",
            "evaluated_runs": len(train_results),
            "eligible_runs": sum(1 for result in train_results if result.get("error") is None),
            "survivors_after_stage": sum(1 for result in train_results if bool(result.get("completed_full_run"))),
        }
    )

    if split is not None:
        holdout_index = len(stages) + 2
        holdout_results = [
            result
            for result in results
            if any(int(item.get("stage_index") or 0) == holdout_index for item in result.get("stage_results", []))
        ]
        summaries.append(
            {
                "stage_index": holdout_index,
                "stage_count": total_stage_count,
                "label": "holdout",
                "evaluated_runs": len(holdout_results),
                "eligible_runs": sum(1 for result in holdout_results if result.get("holdout_error") is None),
                "survivors_after_stage": sum(1 for result in holdout_results if result.get("holdout_error") is None),
                "purge_days": split["purge_days"],
            }
        )

    return summaries


def build_successive_halving_stages(
    *,
    start_date: str,
    end_date: str,
    stage_fractions: list[float] | None,
) -> list[dict[str, Any]]:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    total_days = max(1, (end - start).days + 1)
    stages: list[dict[str, Any]] = []
    seen_end_dates: set[str] = set()

    for fraction in normalize_stage_fractions(stage_fractions):
        stage_days = max(1, ceil(total_days * fraction))
        stage_end = min(end, start + timedelta(days=stage_days - 1))
        stage_end_text = stage_end.isoformat()
        if stage_end_text in seen_end_dates:
            continue
        seen_end_dates.add(stage_end_text)
        stages.append(
            {
                "fraction": float(fraction),
                "label": f"prefix_{stage_end_text}",
                "ranges": [{"start_date": start_date, "end_date": stage_end_text, "label": "prefix"}],
            }
        )

    if not stages or stages[-1]["ranges"][0]["end_date"] != end.isoformat():
        stages.append(
            {
                "fraction": 1.0,
                "label": f"prefix_{end.isoformat()}",
                "ranges": [{"start_date": start_date, "end_date": end.isoformat(), "label": "prefix"}],
            }
        )
    return stages


def resolve_train_holdout_split(
    *,
    start_date: str,
    end_date: str,
    holdout_fraction: float | None,
    holdout_days: int | None,
    purge_days: int,
) -> dict[str, Any] | None:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    total_days = max(1, (end - start).days + 1)
    resolved_holdout_days = holdout_days
    if resolved_holdout_days is None and holdout_fraction is None and total_days >= 252:
        holdout_fraction = 0.2
    if resolved_holdout_days is None and holdout_fraction is not None:
        resolved_holdout_days = max(21, min(126, ceil(total_days * holdout_fraction)))
    if resolved_holdout_days is None:
        return None
    if resolved_holdout_days + purge_days >= total_days:
        return None

    holdout_end = end
    holdout_start = end - timedelta(days=resolved_holdout_days - 1)
    train_end = holdout_start - timedelta(days=purge_days + 1)
    if train_end <= start:
        return None
    effective_fraction = resolved_holdout_days / total_days
    return {
        "enabled": True,
        "train_start": start_date,
        "train_end": train_end.isoformat(),
        "holdout_start": holdout_start.isoformat(),
        "holdout_end": holdout_end.isoformat(),
        "holdout_days": resolved_holdout_days,
        "holdout_fraction": round(effective_fraction, 4),
        "purge_days": purge_days,
    }


def build_regime_halving_stages(
    *,
    instruments: list[str],
    start_date: str,
    end_date: str,
    stage_fractions: list[float] | None,
) -> list[dict[str, Any]]:
    if not instruments:
        return []
    stage_count = len(normalize_stage_fractions(stage_fractions))
    daily = load_daily_market_series(instruments[0], start_date=start_date, end_date=end_date)
    if daily is None or daily.empty or len(daily) < max(40, stage_count * 20):
        return []
    regime_windows = select_regime_windows(daily, stage_count=stage_count)
    if not regime_windows:
        return []

    stages: list[dict[str, Any]] = []
    cumulative: list[dict[str, str]] = []
    total_days = max(1, (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1)
    covered_days = 0
    for window in regime_windows:
        cumulative.append(
            {
                "start_date": window["start_date"],
                "end_date": window["end_date"],
                "label": str(window["label"]),
            }
        )
        covered_days += int(window["days"])
        stages.append(
            {
                "fraction": min(1.0, covered_days / total_days),
                "label": f"regime_{window['label']}",
                "ranges": list(cumulative),
            }
        )
    return stages


def load_daily_market_series(
    instrument_id: str,
    *,
    start_date: str,
    end_date: str,
) -> pd.DataFrame | None:
    raw_symbol = instrument_id.split(".", 1)[0].strip()
    parquet_files: list[Path] = []
    for asset_class in ("equities", "futures", "stocks"):
        symbol_dir = settings.parquet_root / asset_class / raw_symbol
        parquet_files.extend(sorted(symbol_dir.rglob("*.parquet")))
    if not parquet_files:
        return None

    frames: list[pd.DataFrame] = []
    start = pd.Timestamp(start_date, tz="UTC")
    end = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    for parquet_file in parquet_files:
        frame = pd.read_parquet(parquet_file, columns=["timestamp", "close"])
        if frame.empty:
            continue
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame = frame.loc[(frame["timestamp"] >= start) & (frame["timestamp"] < end), ["timestamp", "close"]]
        if frame.empty:
            continue
        frames.append(frame)
    if not frames:
        return None

    combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    combined = combined.set_index("timestamp")
    daily = combined["close"].resample("1D").last().dropna().to_frame(name="close")
    if daily.empty:
        return None
    daily["return"] = daily["close"].pct_change().fillna(0.0)
    daily["rolling_vol"] = daily["return"].rolling(20, min_periods=5).std().fillna(0.0) * (252**0.5)
    return daily


def select_regime_windows(daily: pd.DataFrame, *, stage_count: int) -> list[dict[str, Any]]:
    if daily.empty:
        return []
    total_days = len(daily)
    window_days = max(21, min(63, total_days // max(2, stage_count)))
    if total_days < window_days:
        return []

    candidates: list[dict[str, Any]] = []
    for start_index in range(0, total_days - window_days + 1, window_days):
        window = daily.iloc[start_index : start_index + window_days]
        if len(window) < window_days:
            continue
        start_ts = window.index[0]
        end_ts = window.index[-1]
        period_return = float(window["close"].iloc[-1] / window["close"].iloc[0] - 1.0)
        annualized_vol = float(window["rolling_vol"].mean())
        candidates.append(
            {
                "start_date": start_ts.date().isoformat(),
                "end_date": end_ts.date().isoformat(),
                "days": len(window),
                "period_return": period_return,
                "annualized_vol": annualized_vol,
            }
        )
    if not candidates:
        return []

    selected: list[dict[str, Any]] = []
    selectors = [
        ("high_vol", lambda rows: max(rows, key=lambda row: row["annualized_vol"])),
        ("uptrend", lambda rows: max(rows, key=lambda row: row["period_return"])),
        ("downtrend", lambda rows: min(rows, key=lambda row: row["period_return"])),
    ]
    remaining = list(candidates)
    for label, selector in selectors:
        if not remaining or len(selected) >= stage_count:
            break
        chosen = selector(remaining)
        selected.append({**chosen, "label": label})
        remaining = [candidate for candidate in remaining if candidate["start_date"] != chosen["start_date"]]

    while remaining and len(selected) < stage_count:
        chosen = max(remaining, key=lambda row: abs(row["period_return"]) + row["annualized_vol"])
        selected.append({**chosen, "label": f"diverse_{len(selected) + 1}"})
        remaining = [candidate for candidate in remaining if candidate["start_date"] != chosen["start_date"]]

    return selected[:stage_count]


def aggregate_stage_run_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"error": "No stage results produced", "metrics": None}
    failing = next((result for result in results if result.get("error") is not None), None)
    if failing is not None:
        return {
            "error": str(failing.get("error")),
            "metrics": None,
            "window_results": [to_jsonable(result) for result in results],
        }

    metrics_list = [dict(result.get("metrics") or {}) for result in results]
    trade_counts = [float(metrics.get("num_trades", 0.0)) for metrics in metrics_list]
    total_trades = sum(trade_counts)
    weighted_win_rate = (
        sum(float(metrics.get("win_rate", 0.0)) * trade_count for metrics, trade_count in zip(metrics_list, trade_counts, strict=True))
        / total_trades
        if total_trades
        else 0.0
    )
    aggregate_metrics = {
        "sharpe": sum(float(metrics.get("sharpe", 0.0)) for metrics in metrics_list) / len(metrics_list),
        "sortino": sum(float(metrics.get("sortino", 0.0)) for metrics in metrics_list) / len(metrics_list),
        "max_drawdown": min(float(metrics.get("max_drawdown", 0.0)) for metrics in metrics_list),
        "total_return": sum(float(metrics.get("total_return", 0.0)) for metrics in metrics_list),
        "win_rate": weighted_win_rate,
        "num_trades": int(total_trades),
        "window_count": len(metrics_list),
    }
    sharpe_values = [float(metrics.get("sharpe", 0.0)) for metrics in metrics_list]
    positive_windows = sum(1 for value in sharpe_values if value > 0.0)
    aggregate_metrics["regime_consistency"] = positive_windows / len(sharpe_values)
    aggregate_metrics["sharpe_std"] = float(pd.Series(sharpe_values).std(ddof=0)) if len(sharpe_values) > 1 else 0.0
    return {
        "error": None,
        "metrics": aggregate_metrics,
        "window_results": [to_jsonable(result) for result in results],
    }


def stage_progress(*, stage_index: int, stage_count: int, start: int, end: int) -> int:
    if stage_count <= 1:
        return end
    completed_fraction = (stage_index - 1) / stage_count
    return max(start, min(end, start + int((end - start) * completed_fraction)))


def with_search_metadata(result: dict[str, Any], *, completed_full_run: bool = False) -> dict[str, Any]:
    payload = dict(result)
    payload.setdefault("pruned", False)
    payload.setdefault("prune_reason", None)
    payload.setdefault("pruned_after_stage", None)
    payload["completed_full_run"] = completed_full_run
    payload.setdefault("stage_results", [])
    return payload


def scaled_min_trades(min_trades: int | None, stage_fraction: float) -> int | None:
    if min_trades is None:
        return None
    return max(1, int(ceil(min_trades * stage_fraction)))


def is_stage_eligible(
    *,
    result: dict[str, Any],
    stage_fraction: float,
    min_trades: int | None,
    require_positive_return: bool,
) -> bool:
    if result.get("error") is not None:
        return False

    metrics = result.get("metrics") or {}
    min_trade_threshold = scaled_min_trades(min_trades, stage_fraction)
    if min_trade_threshold is not None and float(metrics.get("num_trades", 0.0)) < min_trade_threshold:
        return False
    return not (require_positive_return and float(metrics.get("total_return", 0.0)) <= 0.0)


def build_prune_reason(
    *,
    result: dict[str, Any],
    stage_fraction: float,
    min_trades: int | None,
    require_positive_return: bool,
) -> str:
    if result.get("error") is not None:
        return str(result.get("error"))

    metrics = result.get("metrics") or {}
    min_trade_threshold = scaled_min_trades(min_trades, stage_fraction)
    if min_trade_threshold is not None and float(metrics.get("num_trades", 0.0)) < min_trade_threshold:
        return f"Insufficient trades for stage budget ({metrics.get('num_trades', 0)} < {min_trade_threshold})"
    if require_positive_return and float(metrics.get("total_return", 0.0)) <= 0.0:
        return "Non-positive return during stage screening"
    return "Pruned during stage screening"


def mark_candidate_pruned(candidate: dict[str, Any], *, stage_index: int, reason: str) -> None:
    candidate["pruned"] = True
    candidate["prune_reason"] = reason
    candidate["pruned_after_stage"] = stage_index
    candidate["completed_full_run"] = False


def expand_parameter_grid(parameter_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not parameter_grid:
        return [{}]

    keys = list(parameter_grid)
    values = [parameter_grid[key] for key in keys]
    for key, candidates in zip(keys, values, strict=True):
        if not candidates:
            raise ValueError(f"Parameter grid entry {key!r} cannot be empty")

    return [
        dict(zip(keys, combination, strict=True))
        for combination in product(*values)
    ]


def count_parameter_grid(parameter_grid: dict[str, list[Any]]) -> int:
    if not parameter_grid:
        return 1
    total = 1
    for key, values in parameter_grid.items():
        if not values:
            raise ValueError(f"Parameter grid entry {key!r} cannot be empty")
        total *= len(values)
    return total


def resolve_optuna_study_name(
    *,
    study_key: str | None,
    strategy_path: str,
    instruments: list[str],
    start_date: str,
    end_date: str,
    objective: str,
) -> str:
    if study_key:
        return study_key
    seed = json.dumps(
        {
            "strategy_path": strategy_path,
            "instruments": instruments,
            "start_date": start_date,
            "end_date": end_date,
            "objective": objective,
        },
        sort_keys=True,
    )
    return f"study-{sha256(seed.encode()).hexdigest()[:16]}"


def sanitize_study_name(name: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in name)
    return slug.strip("-_") or "study"


def window_study_key(study_key: str | None, index: int) -> str | None:
    if not study_key:
        return None
    return f"{study_key}-window-{index}"


def load_optuna_trial_history(study) -> dict[str, dict[str, Any]]:  # type: ignore[no-untyped-def]
    history: dict[str, dict[str, Any]] = {}
    for trial in study.get_trials(deepcopy=False):
        config_key = str(trial.user_attrs.get("config_key") or "")
        if not config_key:
            continue
        state_value = str(trial.state.name).lower()
        history[config_key] = {
            "state": "complete" if state_value == "complete" else state_value,
            "value": trial.value,
        }
    return history


def objective_metric_value(metrics: dict[str, Any], objective: str) -> float:
    value = float(metrics.get(objective, 0.0))
    if objective == "max_drawdown":
        return -abs(value)
    return value


def config_cache_key(config: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(config), sort_keys=True)


def build_walk_forward_windows(
    *,
    start_date: date,
    end_date: date,
    train_days: int,
    test_days: int,
    step_days: int | None = None,
    mode: str = "rolling",
) -> list[WalkForwardWindow]:
    if train_days < 1 or test_days < 1:
        raise ValueError("train_days and test_days must be positive")

    step = step_days or test_days
    if step < 1:
        raise ValueError("step_days must be positive")
    if mode not in {"rolling", "expanding"}:
        raise ValueError("mode must be either 'rolling' or 'expanding'")

    windows: list[WalkForwardWindow] = []
    cursor = start_date
    while True:
        train_start = start_date if mode == "expanding" else cursor
        train_end = cursor + timedelta(days=train_days - 1)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days - 1)
        if test_end > end_date:
            break
        windows.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        cursor = cursor + timedelta(days=step)

    if not windows:
        raise ValueError("No walk-forward windows fit inside the requested date range")
    return windows


def rank_results(results: list[dict[str, Any]], *, objective: str) -> list[dict[str, Any]]:
    def sort_key(result: dict[str, Any]) -> tuple[int, float]:
        if result.get("error") is not None or result.get("holdout_error") is not None:
            return (3, float("-inf"))

        if bool(result.get("pruned")) or not bool(result.get("completed_full_run", True)):
            metrics = result.get("metrics") or {}
            value = float(metrics.get(objective, 0.0))
            if objective == "max_drawdown":
                value = -abs(value)
            return (2, value)

        metrics = result.get("metrics") or {}
        value = float(metrics.get(objective, 0.0))
        if objective == "max_drawdown":
            value = -abs(value)
        selection_basis = str(result.get("selection_basis") or "train")
        priority = 0 if selection_basis == "holdout" else 1
        return (priority, value)

    return sorted(results, key=lambda result: (sort_key(result)[0], -sort_key(result)[1]))


def average_metric(results: list[dict[str, Any]], metric: str) -> float:
    if not results:
        return 0.0
    total = 0.0
    count = 0
    for result in results:
        metrics = result.get("metrics") or {}
        if metric not in metrics:
            continue
        total += float(metrics[metric])
        count += 1
    return total / count if count else 0.0


def min_metric(results: list[dict[str, Any]], metric: str) -> float:
    values = [
        float((result.get("metrics") or {}).get(metric, 0.0))
        for result in results
        if result.get("error") is None
    ]
    return min(values) if values else 0.0


def generalization_gap(
    *,
    in_sample_results: list[dict[str, Any]],
    out_of_sample_results: list[dict[str, Any]],
    metric: str,
) -> float:
    return average_metric(in_sample_results, metric) - average_metric(out_of_sample_results, metric)


def stability_ratio(
    *,
    in_sample_results: list[dict[str, Any]],
    out_of_sample_results: list[dict[str, Any]],
    metric: str,
) -> float:
    in_sample_average = average_metric(in_sample_results, metric)
    if in_sample_average == 0:
        return 0.0
    return average_metric(out_of_sample_results, metric) / in_sample_average


def best_config_consistency(windows: list[dict[str, Any]]) -> float:
    configs = [
        json.dumps(window["best_train_result"]["config"], sort_keys=True)
        for window in windows
        if window.get("best_train_result") is not None
    ]
    if not configs:
        return 0.0

    counts: dict[str, int] = {}
    for config in configs:
        counts[config] = counts.get(config, 0) + 1
    return max(counts.values()) / len(configs)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
