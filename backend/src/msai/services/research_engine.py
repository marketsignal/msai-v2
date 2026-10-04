"""Research engine — parameter sweeps and walk-forward cross-validation.

Ports the core algorithm from the Codex implementation, adapted to use
Claude's :class:`BacktestRunner` and to return result dicts (the worker
layer in Task 8 persists them to the DB).

The engine is **synchronous** — it runs inside ``asyncio.to_thread()``
from the arq worker.  Progress updates flow through an optional
``progress_callback``.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from itertools import product
from math import ceil
from os import cpu_count
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from msai.core.config import settings
from msai.services.nautilus.backtest_runner import BacktestResult, BacktestRunner

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public dataclass (re-exported from Codex for walk-forward windows)
# ---------------------------------------------------------------------------

_METRIC_KEY_MAP: dict[str, str] = {
    "sharpe": "sharpe_ratio",
    "sortino": "sortino_ratio",
    "max_drawdown": "max_drawdown",
    "total_return": "total_return",
    "win_rate": "win_rate",
    "num_trades": "num_trades",
    # Also accept the full key names directly
    "sharpe_ratio": "sharpe_ratio",
    "sortino_ratio": "sortino_ratio",
}


# ---------------------------------------------------------------------------
# Helper functions (module-level, pure)
# ---------------------------------------------------------------------------


def expand_parameter_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Expand a parameter grid into all combinations (cartesian product).

    >>> expand_parameter_grid({"a": [1, 2], "b": [10, 20]})
    [{'a': 1, 'b': 10}, {'a': 1, 'b': 20}, {'a': 2, 'b': 10}, {'a': 2, 'b': 20}]
    """
    if not grid:
        return [{}]

    keys = list(grid)
    values = [grid[key] for key in keys]
    for key, candidates in zip(keys, values, strict=True):
        if not candidates:
            raise ValueError(f"Parameter grid entry {key!r} cannot be empty")

    return [dict(zip(keys, combination, strict=True)) for combination in product(*values)]


def count_parameter_grid(grid: dict[str, list[Any]]) -> int:
    """Count total combinations without materializing them."""
    if not grid:
        return 1
    total = 1
    for key, values in grid.items():
        if not values:
            raise ValueError(f"Parameter grid entry {key!r} cannot be empty")
        total *= len(values)
    return total


def rank_results(
    results: list[dict[str, Any]],
    *,
    objective: str = "sharpe",
) -> list[dict[str, Any]]:
    """Rank usable training results, retaining input order for equal scores.

    Reserved-period diagnostics never affect ranking, including their errors.
    """

    def sort_key(result: dict[str, Any]) -> tuple[int, float]:
        metrics = result.get("train_metrics", result.get("metrics"))
        value = extract_usable_objective_value(metrics, objective)
        if result.get("error") is not None or value is None:
            return (3, float("-inf"))

        if bool(result.get("pruned")) or not bool(result.get("completed_full_run", True)):
            return (2, value)
        if result.get("selection_eligible") is False:
            return (2, value)
        return (0, value)

    return sorted(results, key=lambda r: (sort_key(r)[0], -sort_key(r)[1]))


def build_walk_forward_windows(
    *,
    start_date: date,
    end_date: date,
    train_days: int,
    test_days: int,
    step_days: int | None = None,
    mode: str = "rolling",
) -> list[dict[str, date]]:
    """Generate train/test date windows for walk-forward analysis.

    Returns a list of dicts with keys:
    ``train_start``, ``train_end``, ``test_start``, ``test_end``.
    """
    if train_days < 1 or test_days < 1:
        raise ValueError("train_days and test_days must be positive")

    step = step_days or test_days
    if step < 1:
        raise ValueError("step_days must be positive")
    if mode not in {"rolling", "expanding"}:
        raise ValueError("mode must be either 'rolling' or 'expanding'")

    windows: list[dict[str, date]] = []
    cursor = start_date
    while True:
        train_start = start_date if mode == "expanding" else cursor
        train_end = cursor + timedelta(days=train_days - 1)
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days - 1)
        if test_end > end_date:
            break
        windows.append(
            {
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
            }
        )
        cursor = cursor + timedelta(days=step)

    if not windows:
        raise ValueError("No walk-forward windows fit inside the requested date range")
    return windows


def extract_objective_value(metrics: dict[str, Any], objective: str) -> float:
    """Extract the objective metric value from backtest results.

    Handles both short names (``sharpe``) and full key names
    (``sharpe_ratio``).  For ``max_drawdown``, negates so that "less
    negative" ranks higher (maximization).
    """
    canonical = _METRIC_KEY_MAP.get(objective, objective)
    # Try canonical key first, then the raw objective name
    raw = metrics.get(canonical, metrics.get(objective, 0.0))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0

    if not math.isfinite(value):
        return 0.0

    if objective in {"max_drawdown"}:
        return -abs(value)
    return value


def extract_usable_objective_value(metrics: dict[str, Any] | None, objective: str) -> float | None:
    """Extract a finite objective without inventing evidence for missing metrics."""
    if not metrics:
        return None
    canonical = _METRIC_KEY_MAP.get(objective, objective)
    raw = metrics.get(canonical, metrics.get(objective))
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value):
        return None
    return -abs(value) if objective == "max_drawdown" else value


def mark_training_selection(
    result: dict[str, Any],
    *,
    objective: str,
    min_trades: int | None,
    require_positive_return: bool,
) -> None:
    """Record canonical full-training eligibility for selection and discovery."""
    result["selection_basis"] = "train"
    result.setdefault("train_metrics", result.get("metrics"))
    value = extract_usable_objective_value(result.get("train_metrics"), objective)
    result["objective_value"] = value if result.get("completed_full_run") else None
    reason = None
    if result.get("error") is not None:
        reason = str(result["error"])
        result["objective_value"] = None
    elif result.get("pruned"):
        reason = str(result.get("prune_reason") or "Pruned during training search")
    elif value is None:
        reason = f"Training objective {objective} is missing or nonfinite"
    elif not result.get("completed_full_run"):
        reason = "Full training evaluation did not complete"
    elif not is_stage_eligible(
        result=result,
        stage_fraction=1.0,
        min_trades=min_trades,
        require_positive_return=require_positive_return,
    ):
        reason = build_prune_reason(
            result=result,
            stage_fraction=1.0,
            min_trades=min_trades,
            require_positive_return=require_positive_return,
        )
        result["pruned"] = True
        result["prune_reason"] = reason
    result["selection_eligible"] = reason is None
    result["selection_reason"] = reason


def selection_contract(
    *, policy: str, trial_index_kind: str, selected_trial_index: int | None
) -> dict[str, Any]:
    """Describe the exploratory, training-only automatic selection."""
    return {
        "version": 1,
        "basis": "train",
        "scope": "exploratory",
        "policy": policy,
        "trial_index_kind": trial_index_kind,
        "selected_trial_index": selected_trial_index,
    }


def resolve_search_strategy(
    *,
    requested_strategy: str,
    candidate_count: int,
    start_date: str,
    end_date: str,
) -> str:
    """Pick an automatic search strategy based on grid size and date range."""
    if requested_strategy != "auto":
        return requested_strategy

    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        total_days = max(1, (end - start).days + 1)
    except ValueError:
        total_days = 1

    if candidate_count >= 8 and total_days >= 60:
        return "successive_halving"
    return "grid"


def resolve_train_holdout_split(
    *,
    start_date: str,
    end_date: str,
    holdout_fraction: float | None,
    holdout_days: int | None,
    purge_days: int,
) -> dict[str, Any] | None:
    """Compute train/holdout date split.  Returns None if no holdout."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    total_days = max(1, (end - start).days + 1)

    resolved_holdout_days = holdout_days
    explicit = holdout_days is not None or holdout_fraction is not None
    if resolved_holdout_days is None and holdout_fraction is None and total_days >= 252:
        holdout_fraction = 0.2
    if resolved_holdout_days is None and holdout_fraction is not None:
        resolved_holdout_days = max(21, min(126, ceil(total_days * holdout_fraction)))
    if resolved_holdout_days is None:
        return None
    if resolved_holdout_days + purge_days >= total_days:
        if explicit:
            raise ValueError(
                "Requested holdout plus purge leaves no training range; reduce holdout/purge "
                "or extend the date range"
            )
        return None

    holdout_end = end
    holdout_start = end - timedelta(days=resolved_holdout_days - 1)
    train_end = holdout_start - timedelta(days=purge_days + 1)
    if train_end < start:
        if explicit:
            raise ValueError("Requested holdout plus purge leaves no training range")
        return None
    effective_fraction = resolved_holdout_days / total_days
    return {
        "enabled": True,
        "origin": "explicit" if explicit else "automatic",
        "train_start": start_date,
        "train_end": train_end.isoformat(),
        "holdout_start": holdout_start.isoformat(),
        "holdout_end": holdout_end.isoformat(),
        "holdout_days": resolved_holdout_days,
        "holdout_fraction": round(effective_fraction, 4),
        "purge_days": purge_days,
    }


def normalize_stage_fractions(stage_fractions: list[float] | None) -> list[float]:
    """Normalise and sort stage fractions, ensuring 1.0 is always last."""
    fractions = list(stage_fractions or [0.35, 0.7, 1.0])
    cleaned = sorted({min(1.0, max(0.05, float(v))) for v in fractions})
    if not cleaned or cleaned[-1] < 1.0:
        cleaned.append(1.0)
    return cleaned


def build_successive_halving_stages(
    *,
    start_date: str,
    end_date: str,
    stage_fractions: list[float] | None,
) -> list[dict[str, Any]]:
    """Build screening stages for successive halving."""
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
                "ranges": [
                    {"start_date": start_date, "end_date": stage_end_text, "label": "prefix"}
                ],
            }
        )

    if not stages or stages[-1]["ranges"][0]["end_date"] != end.isoformat():
        stages.append(
            {
                "fraction": 1.0,
                "label": f"prefix_{end.isoformat()}",
                "ranges": [
                    {"start_date": start_date, "end_date": end.isoformat(), "label": "prefix"}
                ],
            }
        )
    return stages


def scaled_min_trades(min_trades: int | None, stage_fraction: float) -> int | None:
    """Scale minimum trade threshold by the stage fraction."""
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
    """Check if a stage result passes eligibility filters."""
    if result.get("error") is not None:
        return False
    metrics = result.get("metrics") or {}
    min_trade_threshold = scaled_min_trades(min_trades, stage_fraction)
    num_trades = extract_usable_objective_value(metrics, "num_trades")
    total_return = extract_usable_objective_value(metrics, "total_return")
    if min_trade_threshold is not None and (num_trades is None or num_trades < min_trade_threshold):
        return False
    return not (require_positive_return and (total_return is None or total_return <= 0.0))


def build_prune_reason(
    *,
    result: dict[str, Any],
    stage_fraction: float,
    min_trades: int | None,
    require_positive_return: bool,
) -> str:
    """Build a human-readable prune reason string."""
    if result.get("error") is not None:
        return str(result.get("error"))
    metrics = result.get("metrics") or {}
    min_trade_threshold = scaled_min_trades(min_trades, stage_fraction)
    num_trades = extract_usable_objective_value(metrics, "num_trades")
    total_return = extract_usable_objective_value(metrics, "total_return")
    if min_trade_threshold is not None and (num_trades is None or num_trades < min_trade_threshold):
        return (
            f"Insufficient trades for stage budget "
            f"({metrics.get('num_trades', 0)} < {min_trade_threshold})"
        )
    if require_positive_return and (total_return is None or total_return <= 0.0):
        return "Non-positive return during stage screening"
    return "Pruned during stage screening"


def mark_candidate_pruned(
    candidate: dict[str, Any],
    *,
    stage_index: int,
    reason: str,
) -> None:
    """Mark a candidate as pruned in-place."""
    candidate["pruned"] = True
    candidate["prune_reason"] = reason
    candidate["pruned_after_stage"] = stage_index
    candidate["completed_full_run"] = False


def stage_progress(
    *,
    stage_index: int,
    stage_count: int,
    start: int,
    end: int,
) -> int:
    """Compute progress percentage for the current stage."""
    if stage_count <= 1:
        return end
    completed_fraction = (stage_index - 1) / stage_count
    return max(start, min(end, start + int((end - start) * completed_fraction)))


def to_jsonable(value: Any) -> Any:
    """Recursively convert a value to JSON-safe primitives."""
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def config_cache_key(config: dict[str, Any]) -> str:
    """Deterministic JSON string for config dedup."""
    return json.dumps(to_jsonable(config), sort_keys=True)


def resolve_optuna_study_name(
    *,
    study_key: str | None,
    strategy_path: str,
    instruments: list[str],
    start_date: str,
    end_date: str,
    objective: str,
) -> str:
    """Build a deterministic Optuna study name."""
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
    """Sanitize a study name for use as a filename."""
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in name)
    return slug.strip("-_") or "study"


def average_metric(results: list[dict[str, Any]], metric: str) -> float:
    """Average a metric across results, skipping missing."""
    if not results:
        return 0.0
    total = 0.0
    count = 0
    for result in results:
        metrics = result.get("metrics") or {}
        value = extract_usable_objective_value(metrics, metric)
        if value is None:
            continue
        total += value
        count += 1
    return total / count if count else 0.0


def min_metric(results: list[dict[str, Any]], metric: str) -> float:
    """Find the minimum value of a metric across results."""
    values = [
        value
        for r in results
        if r.get("error") is None
        and (value := extract_usable_objective_value(r.get("metrics"), metric)) is not None
    ]
    return min(values) if values else 0.0


def generalization_gap(
    *,
    in_sample_results: list[dict[str, Any]],
    out_of_sample_results: list[dict[str, Any]],
    metric: str,
) -> float:
    """Compute the gap between in-sample and out-of-sample performance."""
    return average_metric(in_sample_results, metric) - average_metric(out_of_sample_results, metric)


def stability_ratio(
    *,
    in_sample_results: list[dict[str, Any]],
    out_of_sample_results: list[dict[str, Any]],
    metric: str,
) -> float:
    """Compute the ratio of out-of-sample to in-sample performance."""
    in_avg = average_metric(in_sample_results, metric)
    if in_avg == 0:
        return 0.0
    return average_metric(out_of_sample_results, metric) / in_avg


def best_config_consistency(windows: list[dict[str, Any]]) -> float:
    """Fraction of walk-forward windows that selected the same best config."""
    configs = [
        json.dumps(w["best_train_result"]["config"], sort_keys=True)
        for w in windows
        if w.get("best_train_result") is not None
    ]
    if not configs:
        return 0.0
    counts: dict[str, int] = {}
    for c in configs:
        counts[c] = counts.get(c, 0) + 1
    return max(counts.values()) / len(configs)


def _resolved_parallelism(
    *,
    run_specs_count: int,
    max_parallelism: int | None,
) -> int:
    """Determine how many parallel workers to use."""
    if run_specs_count < 2:
        return 1
    limit = max_parallelism or settings.research_max_parallelism
    return max(1, min(run_specs_count, limit, cpu_count() or 1))


# ---------------------------------------------------------------------------
# Core engine class
# ---------------------------------------------------------------------------


class ResearchEngine:
    """Research engine for parameter sweeps and walk-forward optimization.

    The engine is **synchronous** — it is designed to run inside
    ``asyncio.to_thread()`` from the arq worker.  It delegates actual
    backtest execution to :class:`BacktestRunner` and reports progress
    through an optional callback.
    """

    def __init__(
        self,
        *,
        runner: BacktestRunner | None = None,
    ) -> None:
        self.runner = runner or BacktestRunner()

    # ----- public API -----

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
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run parameter sweep.  Returns results dict with best_config, best_metrics, etc."""
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
        if resolved_min_trades is None and strategy_used in {
            "successive_halving",
            "optuna",
        }:
            resolved_min_trades = 10

        # Optuna branch
        if strategy_used == "optuna":
            return self._run_optuna_parameter_sweep(
                strategy_path=strategy_path,
                base_config=base_config,
                parameter_grid=parameter_grid,
                instruments=instruments,
                start_date=start_date,
                end_date=end_date,
                train_start=train_start,
                train_end=train_end,
                split=split,
                data_path=data_path,
                objective=objective,
                max_parallelism=max_parallelism,
                min_trades=resolved_min_trades,
                require_positive_return=require_positive_return,
                progress_callback=progress_callback,
            )

        # Grid / successive-halving branch
        combinations = expand_parameter_grid(parameter_grid)
        stage_summaries: list[dict[str, Any]] = []

        if strategy_used == "successive_halving":
            results, stage_summaries, survivors = self._run_successive_halving(
                strategy_path=strategy_path,
                base_config=base_config,
                parameter_combinations=combinations,
                instruments=instruments,
                start_date=train_start,
                end_date=train_end,
                data_path=data_path,
                objective=objective,
                max_parallelism=max_parallelism,
                stage_fractions=stage_fractions,
                reduction_factor=reduction_factor,
                min_trades=resolved_min_trades,
                require_positive_return=require_positive_return,
                progress_callback=progress_callback,
            )
        else:
            # Plain grid search
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

        # Full-train evaluation for survivors
        if survivors:
            full_train_results = self._run_candidates(
                candidate_indexes=survivors,
                candidates=results,
                strategy_path=strategy_path,
                instruments=instruments,
                start_date=train_start,
                end_date=train_end,
                data_path=data_path,
            )
            for candidate_index, full_result in full_train_results:
                results[candidate_index]["train_metrics"] = full_result.get("metrics")
                results[candidate_index]["metrics"] = full_result.get("metrics")
                results[candidate_index]["error"] = full_result.get("error")
                results[candidate_index]["completed_full_run"] = full_result.get("error") is None
                results[candidate_index]["selection_basis"] = "train"

        for result in results:
            mark_training_selection(
                result,
                objective=objective,
                min_trades=resolved_min_trades,
                require_positive_return=require_positive_return,
            )

        # Freeze training ranking and winner before any diagnostic execution.
        ranked_results = rank_results(results, objective=objective)
        best_result = self._select_best_result(ranked_results)
        full_period_result = self._evaluate_selected_diagnostics(
            best_result=best_result,
            split=split,
            strategy_path=strategy_path,
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            data_path=data_path,
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "progress": 98,
                    "message": "Finalizing research report",
                    "completed_trials": len(ranked_results),
                    "total_trials": len(ranked_results),
                }
            )

        return {
            "mode": "parameter_sweep",
            "selection": selection_contract(
                policy="best_training",
                trial_index_kind="sweep_result",
                selected_trial_index=0 if best_result is not None else None,
            ),
            "generated_at": datetime.now(UTC).isoformat(),
            "objective": objective,
            "strategy_path": strategy_path,
            "base_config": to_jsonable(base_config),
            "parameter_grid": to_jsonable(parameter_grid),
            "instruments": instruments,
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
                "successful_runs": sum(1 for r in ranked_results if r.get("error") is None),
                "fully_evaluated_runs": sum(
                    1 for r in ranked_results if bool(r.get("completed_full_run"))
                ),
                "pruned_runs": sum(1 for r in ranked_results if bool(r.get("pruned"))),
                "holdout_evaluated_runs": int(split is not None and best_result is not None),
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
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Run walk-forward optimization.  Returns results with per-window metrics."""
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
        total_windows = len(windows)

        for index, window in enumerate(windows, start=1):
            if progress_callback is not None:
                progress_callback(
                    {
                        "progress": 10 + int(80 * ((index - 1) / max(1, total_windows))),
                        "message": (f"Running walk-forward window {index} of {total_windows}"),
                        "stage_index": index,
                        "stage_count": total_windows,
                        "completed_trials": index - 1,
                        "total_trials": total_windows,
                    }
                )

            # Train sweep for this window
            train_report = self.run_parameter_sweep(
                strategy_path=strategy_path,
                base_config=base_config,
                parameter_grid=parameter_grid,
                instruments=instruments,
                start_date=window["train_start"].isoformat(),
                end_date=window["train_end"].isoformat(),
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
            )

            best_train_result = train_report["summary"]["best_result"]
            window_payload: dict[str, Any] = {
                "train_start": window["train_start"].isoformat(),
                "train_end": window["train_end"].isoformat(),
                "test_start": window["test_start"].isoformat(),
                "test_end": window["test_end"].isoformat(),
                "train_results": train_report["results"],
                "train_search": train_report["search"],
                "train_full_period_result": train_report["summary"]["full_period_result"],
                "best_train_result": best_train_result,
                "test_result": None,
            }

            if best_train_result is not None:
                in_sample_results.append(best_train_result)
                # Test on out-of-sample window
                test_result = self._run_one(
                    strategy_path=strategy_path,
                    config={**base_config, **dict(best_train_result["config"])},
                    instruments=instruments,
                    start_date=window["test_start"].isoformat(),
                    end_date=window["test_end"].isoformat(),
                    data_path=data_path,
                )
                window_payload["test_result"] = test_result
                if test_result.get("error") is None:
                    out_of_sample_results.append(test_result)

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

        summary = {
            "mode": mode,
            "best_result": payload_windows[-1]["best_train_result"] if payload_windows else None,
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
            "selection": selection_contract(
                policy="latest_window_training",
                trial_index_kind="walk_forward_window",
                selected_trial_index=(
                    len(payload_windows) - 1 if summary["best_result"] is not None else None
                ),
            ),
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
            "instruments": instruments,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "train_days": train_days,
            "test_days": test_days,
            "step_days": step_days or test_days,
            "walk_forward_mode": mode,
            "summary": summary,
            "windows": payload_windows,
        }

    # ----- private helpers -----

    def _evaluate_selected_diagnostics(
        self,
        *,
        best_result: dict[str, Any] | None,
        split: dict[str, Any] | None,
        strategy_path: str,
        instruments: list[str],
        start_date: str,
        end_date: str,
        data_path: Path,
    ) -> dict[str, Any] | None:
        """Evaluate only the frozen training choice; keep diagnostics separate."""
        if split is None or best_result is None:
            return None
        holdout = self._run_one(
            strategy_path=strategy_path,
            config=dict(best_result["config"]),
            instruments=instruments,
            start_date=split["holdout_start"],
            end_date=split["holdout_end"],
            data_path=data_path,
        )
        best_result["holdout_metrics"] = holdout.get("metrics")
        best_result["holdout_error"] = holdout.get("error")
        return self._run_one(
            strategy_path=strategy_path,
            config=dict(best_result["config"]),
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            data_path=data_path,
        )

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
        """Run a single backtest and return a result dict."""
        try:
            result: BacktestResult = self.runner.run(
                strategy_file=strategy_path,
                strategy_config=config,
                instrument_ids=instruments,
                start_date=start_date,
                end_date=end_date,
                catalog_path=data_path,
                timeout_seconds=settings.backtest_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "backtest_failed",
                strategy_path=strategy_path,
                error=str(exc),
            )
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

    def _run_candidates(
        self,
        *,
        candidate_indexes: list[int],
        candidates: list[dict[str, Any]],
        strategy_path: str,
        instruments: list[str],
        start_date: str,
        end_date: str,
        data_path: Path,
    ) -> list[tuple[int, dict[str, Any]]]:
        """Run backtests for a list of candidate indexes, serially."""
        return [
            (
                idx,
                self._run_one(
                    strategy_path=strategy_path,
                    config=dict(candidates[idx]["config"]),
                    instruments=instruments,
                    start_date=start_date,
                    end_date=end_date,
                    data_path=data_path,
                ),
            )
            for idx in candidate_indexes
        ]

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
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int]]:
        """Run successive halving: progressive filtering of candidates."""
        if not parameter_combinations:
            return [], [], []

        stages = build_successive_halving_stages(
            start_date=start_date,
            end_date=end_date,
            stage_fractions=stage_fractions,
        )

        candidates: list[dict[str, Any]] = [
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

        for stage_idx, stage_def in enumerate(stages, start=1):
            if not survivors:
                break

            if progress_callback is not None:
                progress_callback(
                    {
                        "progress": stage_progress(
                            stage_index=stage_idx,
                            stage_count=len(stages),
                            start=10,
                            end=90,
                        ),
                        "message": (
                            f"Stage {stage_idx}/{len(stages)}: "
                            f"evaluating {len(survivors)} candidates"
                        ),
                        "stage_index": stage_idx,
                        "stage_count": len(stages),
                        "completed_trials": total_candidates - len(survivors),
                        "total_trials": total_candidates,
                    }
                )

            stage_range = stage_def["ranges"][0]
            stage_results = self._run_candidates(
                candidate_indexes=survivors,
                candidates=candidates,
                strategy_path=strategy_path,
                instruments=instruments,
                start_date=str(stage_range["start_date"]),
                end_date=str(stage_range["end_date"]),
                data_path=data_path,
            )

            eligible_indexes: list[int] = []
            eligible_results: list[dict[str, Any]] = []

            for candidate_index, result in stage_results:
                candidates[candidate_index]["stage_results"].append(
                    to_jsonable(
                        {
                            "stage_index": stage_idx,
                            "stage_count": len(stages),
                            "fraction": stage_def["fraction"],
                            "label": stage_def["label"],
                            "error": result.get("error"),
                            "metrics": result.get("metrics"),
                        }
                    )
                )
                candidates[candidate_index]["metrics"] = result.get("metrics")
                candidates[candidate_index]["error"] = result.get("error")

                if extract_usable_objective_value(result.get("metrics"), objective) is None:
                    continue
                if is_stage_eligible(
                    result=result,
                    stage_fraction=stage_def["fraction"],
                    min_trades=min_trades,
                    require_positive_return=require_positive_return,
                ):
                    eligible_indexes.append(candidate_index)
                    eligible_results.append(result)
                elif result.get("error") is None:
                    mark_candidate_pruned(
                        candidates[candidate_index],
                        stage_index=stage_idx,
                        reason=build_prune_reason(
                            result=result,
                            stage_fraction=stage_def["fraction"],
                            min_trades=min_trades,
                            require_positive_return=require_positive_return,
                        ),
                    )

            # Rank eligible candidates and reduce
            ranked_stage_pairs = rank_results(
                [
                    {"candidate_index": ci, **r}
                    for ci, r in zip(eligible_indexes, eligible_results, strict=True)
                ],
                objective=objective,
            )
            ranked_stage_indexes = [int(r["candidate_index"]) for r in ranked_stage_pairs]

            if stage_idx == len(stages):
                survivors = ranked_stage_indexes
            else:
                keep_count = min(
                    len(ranked_stage_indexes),
                    max(1, ceil(len(ranked_stage_indexes) / reduction_factor)),
                )
                next_survivors = ranked_stage_indexes[:keep_count]
                for ci in ranked_stage_indexes[keep_count:]:
                    mark_candidate_pruned(
                        candidates[ci],
                        stage_index=stage_idx,
                        reason=(f"Pruned by successive halving after stage {stage_idx}"),
                    )
                survivors = next_survivors

            stage_summaries.append(
                {
                    "stage_index": stage_idx,
                    "stage_count": len(stages),
                    "fraction": stage_def["fraction"],
                    "label": stage_def["label"],
                    "evaluated_runs": len(stage_results),
                    "eligible_runs": len(ranked_stage_indexes),
                    "survivors_after_stage": len(survivors),
                }
            )

        return (
            [to_jsonable(c) for c in candidates],
            stage_summaries,
            survivors,
        )

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
        min_trades: int | None,
        require_positive_return: bool,
        progress_callback: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        """Run an Optuna-driven parameter sweep."""
        if not settings.optuna_enabled:
            raise ValueError("Optuna search is disabled by configuration")

        from optuna import create_study
        from optuna.samplers import TPESampler
        from optuna.storages import JournalStorage
        from optuna.storages.journal import JournalFileBackend
        from optuna.trial import TrialState

        study_prefix = resolve_optuna_study_name(
            study_key=None,
            strategy_path=strategy_path,
            instruments=instruments,
            start_date=start_date,
            end_date=end_date,
            objective=objective,
        )
        # Until immutable experiment identity exists, every invocation has isolated feedback.
        study_name = f"{study_prefix}-train-v1-{uuid4().hex}"
        settings.optuna_root.mkdir(parents=True, exist_ok=True)
        storage_path = settings.optuna_root / f"{sanitize_study_name(study_name)}.journal"

        study = create_study(
            study_name=study_name,
            direction="maximize",
            sampler=TPESampler(),
            storage=JournalStorage(JournalFileBackend(str(storage_path))),
            load_if_exists=False,
        )

        grid_limit = count_parameter_grid(parameter_grid)
        target_trials = min(int(settings.optuna_max_trials), grid_limit)
        history: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []
        terminal_trials = 0

        while terminal_trials < target_trials and len(history) < grid_limit:
            trial = study.ask()
            params = {
                key: trial.suggest_categorical(key, list(vals))
                for key, vals in sorted(parameter_grid.items())
            }
            candidate_config = {**base_config, **params}
            cache_key = config_cache_key(candidate_config)
            trial.set_user_attr("config_key", cache_key)

            cached = history.get(cache_key)
            if cached is not None:
                if cached["state"] == "complete" and cached["value"] is not None:
                    study.tell(trial, float(cached["value"]))
                elif cached["state"] == "pruned":
                    study.tell(trial, state=TrialState.PRUNED)
                else:
                    study.tell(trial, state=TrialState.FAIL)
                terminal_trials += 1  # advance loop to avoid infinite spin
                continue

            history[cache_key] = {"state": "pending", "value": None}

            if progress_callback is not None:
                progress_callback(
                    {
                        "progress": 10 + int(70 * (terminal_trials / max(1, target_trials))),
                        "message": f"Optuna trial {terminal_trials + 1}/{target_trials}",
                        "completed_trials": terminal_trials,
                        "total_trials": target_trials,
                    }
                )

            # Train run
            train_result = self._run_one(
                strategy_path=strategy_path,
                config=candidate_config,
                instruments=instruments,
                start_date=train_start,
                end_date=train_end,
                data_path=data_path,
            )

            candidate: dict[str, Any] = {
                "config": to_jsonable(candidate_config),
                "start_date": train_start,
                "end_date": train_end,
                "error": train_result.get("error"),
                "metrics": train_result.get("metrics"),
                "train_metrics": train_result.get("metrics"),
                "pruned": False,
                "prune_reason": None,
                "pruned_after_stage": None,
                "completed_full_run": train_result.get("error") is None,
                "selection_basis": "train",
                "stage_results": [],
            }

            mark_training_selection(
                candidate,
                objective=objective,
                min_trades=min_trades,
                require_positive_return=require_positive_return,
            )

            # Report to Optuna
            if candidate.get("pruned"):
                study.tell(trial, state=TrialState.PRUNED)
                history[cache_key] = {"state": "pruned", "value": None}
            elif not candidate["selection_eligible"]:
                study.tell(trial, state=TrialState.FAIL)
                history[cache_key] = {"state": "fail", "value": None}
            else:
                obj_val = candidate["objective_value"]
                study.tell(trial, obj_val)
                history[cache_key] = {"state": "complete", "value": obj_val}

            results.append(candidate)
            terminal_trials += 1

        ranked_results = rank_results(results, objective=objective)
        best_result = self._select_best_result(ranked_results)

        full_period_result = self._evaluate_selected_diagnostics(
            best_result=best_result,
            split=split,
            strategy_path=strategy_path,
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

        return {
            "mode": "parameter_sweep",
            "selection": selection_contract(
                policy="best_training",
                trial_index_kind="sweep_result",
                selected_trial_index=0 if best_result is not None else None,
            ),
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
                "min_trades": min_trades,
                "require_positive_return": require_positive_return,
                "holdout": split,
                "study_name": study_name,
                "storage_path": str(storage_path),
                "target_trials": target_trials,
                "evaluated_trials": len(results),
            },
            "summary": {
                "total_runs": len(ranked_results),
                "successful_runs": sum(1 for r in ranked_results if r.get("error") is None),
                "fully_evaluated_runs": sum(
                    1 for r in ranked_results if bool(r.get("completed_full_run"))
                ),
                "pruned_runs": sum(1 for r in ranked_results if bool(r.get("pruned"))),
                "holdout_evaluated_runs": int(split is not None and best_result is not None),
                "best_result": best_result,
                "full_period_result": full_period_result,
            },
            "results": ranked_results,
        }

    @staticmethod
    def _select_best_result(
        ranked_results: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Pick the first canonically eligible full-training result."""
        return next(
            (r for r in ranked_results if r.get("selection_eligible") is True),
            None,
        )
