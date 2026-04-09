#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from itertools import product
from pathlib import Path

from msai.core.config import settings
from msai.services.research_engine import (
    ResearchEngine,
    average_metric,
    best_config_consistency,
    build_walk_forward_windows,
    generalization_gap,
    min_metric,
    rank_results,
    stability_ratio,
)
from msai.services.strategy_registry import StrategyRegistry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run MSAI research jobs from a real file entrypoint.")
    parser.add_argument("--mode", choices=("sweep", "walk-forward"), required=True)
    parser.add_argument("--strategy", required=True, help="Strategy registry name, for example user.slope_ma_breakout")
    parser.add_argument("--instruments", required=True, help="Comma-separated canonical instrument ids")
    parser.add_argument("--start", required=True, help="Backtest start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="Backtest end date (YYYY-MM-DD)")
    parser.add_argument("--base-config", default="{}", help="JSON object for base strategy config")
    parser.add_argument("--grid", required=True, help="JSON object for the parameter grid")
    parser.add_argument("--objective", default="sharpe")
    parser.add_argument("--max-parallelism", type=int, default=1)
    parser.add_argument("--output-path", default="", help="Optional path for the saved report JSON")
    parser.add_argument("--runner", choices=("engine", "fresh-cli"), default="engine")
    parser.add_argument("--train-days", type=int, default=252)
    parser.add_argument("--test-days", type=int, default=63)
    parser.add_argument("--step-days", type=int, default=63)
    parser.add_argument("--walk-forward-mode", choices=("rolling", "expanding"), default="expanding")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    registry = StrategyRegistry(settings.strategies_root)
    discovered = {strategy.name: strategy for strategy in registry.discover()}
    selected = discovered.get(args.strategy)
    if selected is None:
        raise SystemExit(f"Unknown strategy: {args.strategy}")

    engine = ResearchEngine()
    instruments = [value.strip() for value in args.instruments.split(",") if value.strip()]
    base_config = json.loads(args.base_config)
    parameter_grid = json.loads(args.grid)
    strategy_path = str(registry.root / selected.file_path)
    output_path = Path(args.output_path) if args.output_path else None

    if args.mode == "sweep" and args.runner == "engine":
        report = engine.run_parameter_sweep(
            strategy_path=strategy_path,
            base_config=base_config,
            parameter_grid=parameter_grid,
            instruments=instruments,
            start_date=args.start,
            end_date=args.end,
            data_path=settings.nautilus_catalog_root,
            objective=args.objective,
            max_parallelism=args.max_parallelism,
        )
    elif args.mode == "walk-forward" and args.runner == "engine":
        report = engine.run_walk_forward(
            strategy_path=strategy_path,
            base_config=base_config,
            parameter_grid=parameter_grid,
            instruments=instruments,
            start_date=date.fromisoformat(args.start),
            end_date=date.fromisoformat(args.end),
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            mode=args.walk_forward_mode,
            data_path=settings.nautilus_catalog_root,
            objective=args.objective,
            max_parallelism=args.max_parallelism,
        )
    elif args.mode == "sweep":
        report = run_parameter_sweep_fresh_cli(
            strategy_name=args.strategy,
            instruments=instruments,
            start_date=args.start,
            end_date=args.end,
            base_config=base_config,
            parameter_grid=parameter_grid,
            objective=args.objective,
        )
    else:
        report = run_walk_forward_fresh_cli(
            strategy_name=args.strategy,
            instruments=instruments,
            start_date=date.fromisoformat(args.start),
            end_date=date.fromisoformat(args.end),
            base_config=base_config,
            parameter_grid=parameter_grid,
            objective=args.objective,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            mode=args.walk_forward_mode,
        )

    report_path = engine.save_report(report, output_path)
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "summary": report["summary"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def expand_parameter_grid(parameter_grid: dict[str, list[object]]) -> list[dict[str, object]]:
    if not parameter_grid:
        return [{}]
    keys = list(parameter_grid)
    values = [parameter_grid[key] for key in keys]
    return [dict(zip(keys, combination, strict=True)) for combination in product(*values)]


def run_parameter_sweep_fresh_cli(
    *,
    strategy_name: str,
    instruments: list[str],
    start_date: str,
    end_date: str,
    base_config: dict[str, object],
    parameter_grid: dict[str, list[object]],
    objective: str,
) -> dict[str, object]:
    combinations = expand_parameter_grid(parameter_grid)
    results = []
    for params in combinations:
        config = {**base_config, **params}
        result = run_backtest_via_cli(
            strategy_name=strategy_name,
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            config=config,
        )
        results.append(result)
    ranked_results = rank_results(results, objective=objective)
    return {
        "mode": "parameter_sweep",
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "objective": objective,
        "strategy_path": strategy_name,
        "base_config": base_config,
        "parameter_grid": parameter_grid,
        "instruments": instruments,
        "start_date": start_date,
        "end_date": end_date,
        "summary": {
            "total_runs": len(ranked_results),
            "successful_runs": sum(1 for result in ranked_results if result.get("error") is None),
            "best_result": ranked_results[0] if ranked_results else None,
        },
        "results": ranked_results,
    }


def run_walk_forward_fresh_cli(
    *,
    strategy_name: str,
    instruments: list[str],
    start_date: date,
    end_date: date,
    base_config: dict[str, object],
    parameter_grid: dict[str, list[object]],
    objective: str,
    train_days: int,
    test_days: int,
    step_days: int,
    mode: str,
) -> dict[str, object]:
    windows = build_walk_forward_windows(
        start_date=start_date,
        end_date=end_date,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        mode=mode,
    )

    payload_windows: list[dict[str, object]] = []
    out_of_sample_results: list[dict[str, object]] = []
    in_sample_results: list[dict[str, object]] = []
    for window in windows:
        train_report = run_parameter_sweep_fresh_cli(
            strategy_name=strategy_name,
            instruments=instruments,
            start_date=window.train_start.isoformat(),
            end_date=window.train_end.isoformat(),
            base_config=base_config,
            parameter_grid=parameter_grid,
            objective=objective,
        )
        best_train_result = train_report["summary"]["best_result"]
        window_payload: dict[str, object] = {
            "train_start": window.train_start.isoformat(),
            "train_end": window.train_end.isoformat(),
            "test_start": window.test_start.isoformat(),
            "test_end": window.test_end.isoformat(),
            "train_results": train_report["results"],
            "best_train_result": best_train_result,
            "test_result": None,
        }
        if best_train_result is not None:
            in_sample_results.append(best_train_result)
            test_result = run_backtest_via_cli(
                strategy_name=strategy_name,
                instruments=instruments,
                start_date=window.test_start.isoformat(),
                end_date=window.test_end.isoformat(),
                config={**base_config, **dict(best_train_result["config"])},
            )
            window_payload["test_result"] = test_result
            if test_result.get("error") is None:
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
        "mode": "walk_forward",
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        "objective": objective,
        "strategy_path": strategy_name,
        "base_config": base_config,
        "parameter_grid": parameter_grid,
        "instruments": instruments,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "train_days": train_days,
        "test_days": test_days,
        "step_days": step_days,
        "walk_forward_mode": mode,
        "summary": summary,
        "windows": payload_windows,
    }


def run_backtest_via_cli(
    *,
    strategy_name: str,
    instruments: list[str],
    start_date: str,
    end_date: str,
    config: dict[str, object],
) -> dict[str, object]:
    command = [
        sys.executable,
        "-m",
        "msai.cli",
        "backtest",
        "run",
        strategy_name,
        ",".join(instruments),
        start_date,
        end_date,
        "--config-json",
        json.dumps(config),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(settings.project_root / "backend"),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        return {
            "config": config,
            "start_date": start_date,
            "end_date": end_date,
            "error": exc.stderr.strip() or exc.stdout.strip() or str(exc),
            "metrics": None,
        }

    output = completed.stdout.strip()
    try:
        metrics = json.loads(output)
    except json.JSONDecodeError as exc:
        return {
            "config": config,
            "start_date": start_date,
            "end_date": end_date,
            "error": f"Unable to decode CLI metrics output: {exc}",
            "metrics": None,
        }
    return {
        "config": config,
        "start_date": start_date,
        "end_date": end_date,
        "error": None,
        "metrics": metrics,
    }
