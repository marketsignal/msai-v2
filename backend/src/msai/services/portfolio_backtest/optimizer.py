"""Full-mode optimizer driver — walk-forward + Optuna for portfolio backtests.

Reuses module-level helpers from :mod:`msai.services.research_engine`
(``build_walk_forward_windows`` for window construction) but rolls its own
Optuna ask/tell loop because :meth:`ResearchEngine.run_walk_forward` is
strategy-singular by construction (its ``strategy_path: str`` parameter
accepts a single strategy file only).

Cancellation: ``cancel_check`` is consulted at the top of each trial; on
``True`` the trial loop exits cleanly so the journal never sees a pending
trial on resume.

Safety caps: applied as Optuna search-space upper bounds (clip) AND as a
post-evaluation rejection in the trial body (catches derived violations
emerging from per-strategy weight combinations).

The trial body is injected via ``portfolio_backtest_fn`` so this module stays
free of :class:`~msai.services.portfolio.orchestration.PortfolioService`
imports — Task F1 wires it to the real PortfolioService-level backtest call,
while unit tests pass a stub.
"""

from __future__ import annotations

import contextlib
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from msai.core.config import settings
from msai.services.portfolio_backtest.objectives import objective_score
from msai.services.portfolio_backtest.safety_caps import (
    SafetyCaps,
    SafetyCapsBreach,
    enforce_caps,
)
from msai.services.research_engine import build_walk_forward_windows, sanitize_study_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from msai.models.portfolio_enums import PortfolioObjective


@dataclass(frozen=True)
class PortfolioOptimizationResult:
    """Output of a Full-mode portfolio optimization run.

    ``walk_forward_payload`` is the JSON-serialisable bundle persisted on the
    :class:`PortfolioRun` row; ``optimization_trace`` carries one entry per
    trial (params + scores or prune/fail reason).
    """

    is_metric: float
    oos_metric: float
    generalization_gap: float
    stability_ratio: float
    best_config: dict[str, Any]
    optimization_trace: list[dict[str, Any]] = field(default_factory=list)
    walk_forward_payload: dict[str, Any] = field(default_factory=dict)


def build_search_space(caps: SafetyCaps) -> dict[str, dict[str, Any]]:
    """Construct the Optuna search-space spec, clipped to safety caps.

    Each entry maps a parameter name to its ``suggest_float`` bounds. The
    ``high`` value is pinned to the corresponding cap so Optuna's sampler can
    never propose a value above it.
    """
    return {
        "leverage": {"low": 0.1, "high": float(caps.max_leverage), "log": False},
        "position_size": {
            "low": 0.0,
            "high": float(caps.max_position_size) if caps.max_position_size is not None else 1.0,
            "log": False,
        },
        # Additional risk-policy parameters can be added here as the search
        # space grows. Each new entry must define its own safety-cap binding.
    }


def _study_storage_path(study_name: str) -> str:
    """Return the JournalFileBackend storage path for ``study_name``.

    Uses :func:`research_engine.sanitize_study_name` so the file segment is
    safe for the filesystem; mirrors ``ResearchEngine._run_optuna_search``.
    """
    settings.optuna_root.mkdir(parents=True, exist_ok=True)
    return str(settings.optuna_root / f"{sanitize_study_name(study_name)}.journal")


def run_portfolio_walk_forward(
    *,
    portfolio_id: str,
    member_strategy_ids: list[str],
    allocator_name: str,
    objective: PortfolioObjective,
    safety_caps: SafetyCaps,
    start_date: date,
    end_date: date,
    initial_capital: float,
    train_days: int = 252,
    test_days: int = 63,
    step_days: int = 63,
    n_trials: int = 100,
    progress_callback: Callable[[int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    portfolio_backtest_fn: Callable[..., dict[str, Any]] | None = None,
) -> PortfolioOptimizationResult:
    """Run walk-forward portfolio optimization, returning a consolidated result.

    Calls :func:`build_walk_forward_windows` directly — we do NOT use
    :meth:`ResearchEngine.run_walk_forward` because that path is
    strategy-singular by construction.

    Approach:
      1. Build train/test windows via ``build_walk_forward_windows``.
      2. For each window, drive an Optuna study with ``study.ask()`` /
         ``study.tell()``. Each trial: suggest risk-policy params (clipped
         by ``safety_caps``), run a portfolio backtest on the train window,
         apply post-evaluation cap checks, score the IS metrics via
         :func:`objective_score`, then evaluate the same params on the test
         window for OOS.
      3. Aggregate IS / OOS scores with plain ``sum / len`` averaging.
      4. Return the consolidated :class:`PortfolioOptimizationResult`.

    ``portfolio_backtest_fn`` is injected by Task F1 — it is the real
    PortfolioService-level backtest call. Defaulting to ``None`` lets unit
    tests pass a stub. Invoking the optimizer without a stub raises
    :class:`NotImplementedError` from inside the trial body.
    """
    # Local imports keep Optuna out of the module-level dependency graph; it is
    # heavy to import and not needed for ``build_search_space`` or the dataclass
    # exports.
    import optuna
    from optuna.samplers import TPESampler
    from optuna.storages import JournalStorage
    from optuna.storages.journal import JournalFileBackend
    from optuna.trial import TrialState

    windows = build_walk_forward_windows(
        start_date=start_date,
        end_date=end_date,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        mode="rolling",
    )

    in_sample_scores: list[float] = []
    out_of_sample_scores: list[float] = []
    trace: list[dict[str, Any]] = []
    best_config: dict[str, Any] = {}

    search_space = build_search_space(safety_caps)
    # Stable, journal-resumable study name. ``resolve_optuna_study_name`` from
    # research_engine.py is strategy-singular; we roll our own here so the
    # portfolio-shape (portfolio_id + objective) drives the journal identity.
    study_name = f"portfolio-{portfolio_id}-{objective.value}"
    study = optuna.create_study(
        study_name=study_name,
        direction="maximize",
        storage=JournalStorage(JournalFileBackend(_study_storage_path(study_name))),
        sampler=TPESampler(constant_liar=True),
        load_if_exists=True,
    )

    total_windows = len(windows)
    for w_idx, window in enumerate(windows, start=1):
        if cancel_check and cancel_check():
            break
        # Distribute the trial budget across windows; minimum one per window.
        trials_this_window = max(1, n_trials // total_windows)
        for _t_idx in range(trials_this_window):
            if cancel_check and cancel_check():
                break
            trial = study.ask()
            params: dict[str, float] = {}
            # ``told`` tracks whether the trial body completed ``study.tell``
            # for this trial.  If an unexpected exception escapes BEFORE we
            # reach an explicit tell (FAIL / PRUNED / score), the ``finally``
            # block tells FAIL so Optuna never has a phantom RUNNING trial
            # in the journal.  Without this guard a mid-trial process
            # interrupt would resume into a study that thinks a trial is
            # still in-flight, blocking subsequent ``ask()`` calls until
            # the trial state is manually scrubbed.
            told = False
            try:
                params = {
                    name: trial.suggest_float(
                        name,
                        spec["low"],
                        spec["high"],
                        log=bool(spec.get("log", False)),
                    )
                    for name, spec in search_space.items()
                }
                if portfolio_backtest_fn is None:
                    raise NotImplementedError("portfolio_backtest_fn must be injected by Task F1")
                # IS evaluation on the train window
                is_metrics = portfolio_backtest_fn(
                    member_strategy_ids=member_strategy_ids,
                    allocator_name=allocator_name,
                    risk_params=params,
                    start_date=window["train_start"],
                    end_date=window["train_end"],
                    initial_capital=initial_capital,
                )
                # Belt-and-suspenders: catch derived-leverage / drawdown caps
                # that the search-space clip can't bound (because they emerge
                # from the combination of per-strategy weights, not from any
                # single parameter).
                enforce_caps(
                    safety_caps,
                    total_leverage=float(
                        is_metrics.get("total_leverage", params.get("leverage", 1.0))
                    ),
                    max_position=float(is_metrics.get("max_position", 0.0)),
                    observed_max_dd=abs(float(is_metrics.get("max_drawdown", 0.0))),
                )
                is_score = objective_score(is_metrics, objective)
                in_sample_scores.append(is_score)

                # OOS evaluation with the same params on the test window
                oos_metrics = portfolio_backtest_fn(
                    member_strategy_ids=member_strategy_ids,
                    allocator_name=allocator_name,
                    risk_params=params,
                    start_date=window["test_start"],
                    end_date=window["test_end"],
                    initial_capital=initial_capital,
                )
                oos_score = objective_score(oos_metrics, objective)
                out_of_sample_scores.append(oos_score)

                trace.append(
                    {
                        "trial": trial.number,
                        "window_index": w_idx,
                        "params": params,
                        "is_score": is_score,
                        "oos_score": oos_score,
                        "is_metrics": is_metrics,
                        "oos_metrics": oos_metrics,
                    }
                )
                study.tell(trial, is_score)
                told = True
                # Walk-forward best-config selection: pick the trial whose
                # worst-of-IS-OOS is highest ("robust" selection — the
                # standard defense against overfitting on the IS window).
                # An IS-only criterion would happily promote trials that
                # crater on OOS, which is the exact failure mode walk-forward
                # is supposed to guard against.
                robust_score = min(is_score, oos_score)
                if not best_config or robust_score > best_config.get("score", -math.inf):
                    best_config = {"score": robust_score, "params": params}
            except SafetyCapsBreach as breach:
                study.tell(trial, state=TrialState.PRUNED)
                told = True
                trace.append(
                    {
                        "trial": trial.number,
                        "window_index": w_idx,
                        "params": params,
                        "pruned": "safety_cap",
                        "reason": str(breach),
                    }
                )
            except Exception as exc:  # noqa: BLE001 — Optuna requires telling FAIL
                study.tell(trial, state=TrialState.FAIL)
                told = True
                trace.append(
                    {
                        "trial": trial.number,
                        "window_index": w_idx,
                        "params": params,
                        "error": str(exc),
                    }
                )
            finally:
                # Last-resort guarantee no phantom RUNNING trials remain in
                # the journal.  If an exception escapes the try/except above
                # without setting ``told`` (e.g. an OperationalError raised
                # while Optuna itself was telling the result), this final
                # tell-FAIL is the only thing standing between us and a
                # journal that can't be resumed cleanly.  Best-effort: a
                # second exception inside ``study.tell`` here is swallowed
                # so the outer ``finally`` cleanup completes — the caller
                # still sees the original visible failure.
                if not told:
                    with contextlib.suppress(Exception):
                        study.tell(trial, state=TrialState.FAIL)
        if progress_callback:
            progress_callback(int(100 * w_idx / total_windows), f"window {w_idx}/{total_windows}")

    is_avg = sum(in_sample_scores) / len(in_sample_scores) if in_sample_scores else 0.0
    oos_avg = sum(out_of_sample_scores) / len(out_of_sample_scores) if out_of_sample_scores else 0.0
    stability = (oos_avg / is_avg) if is_avg else 0.0

    return PortfolioOptimizationResult(
        is_metric=is_avg,
        oos_metric=oos_avg,
        generalization_gap=is_avg - oos_avg,
        stability_ratio=stability,
        best_config=best_config.get("params", {}),
        optimization_trace=trace,
        walk_forward_payload={
            "windows": [{k: v.isoformat() for k, v in w.items()} for w in windows],
            "in_sample_scores": in_sample_scores,
            "out_of_sample_scores": out_of_sample_scores,
        },
    )
