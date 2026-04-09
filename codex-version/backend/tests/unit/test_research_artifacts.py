from __future__ import annotations

import json
from pathlib import Path

import pytest

from msai.services.research_artifacts import (
    ResearchArtifactService,
    ResearchPromotionError,
)


def test_list_reports_summarizes_saved_parameter_sweeps(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "sweep-report.json",
        {
            "mode": "parameter_sweep",
            "generated_at": "2026-04-07T12:00:00Z",
            "objective": "sharpe",
            "strategy_path": "/workspace/strategies/example/mean_reversion.py",
            "instruments": ["AAPL.EQUS"],
            "start_date": "2026-03-01",
            "end_date": "2026-03-31",
            "summary": {"total_runs": 2, "successful_runs": 2},
            "results": [
                {"config": {"lookback": 20}, "metrics": {"sharpe": 2.0}, "error": None},
                {"config": {"lookback": 10}, "metrics": {"sharpe": 1.0}, "error": None},
            ],
        },
    )

    service = ResearchArtifactService(root=tmp_path)

    reports = service.list_reports()

    assert len(reports) == 1
    assert reports[0].id == "sweep-report"
    assert reports[0].strategy_name == "example.mean_reversion"
    assert reports[0].best_config == {"lookback": 20}
    assert reports[0].candidate_count == 2


def test_select_candidate_prefers_best_successful_walk_forward_window(tmp_path: Path) -> None:
    service = ResearchArtifactService(root=tmp_path)
    report = {
        "mode": "walk_forward",
        "objective": "sharpe",
        "windows": [
            {
                "train_start": "2026-01-01",
                "train_end": "2026-01-31",
                "test_start": "2026-02-01",
                "test_end": "2026-02-10",
                "best_train_result": {"config": {"lookback": 10}, "metrics": {"sharpe": 1.2}, "error": None},
                "test_result": {"metrics": {"sharpe": 0.8}, "error": None},
            },
            {
                "train_start": "2026-01-11",
                "train_end": "2026-02-10",
                "test_start": "2026-02-11",
                "test_end": "2026-02-20",
                "best_train_result": {"config": {"lookback": 20}, "metrics": {"sharpe": 1.8}, "error": None},
                "test_result": {"metrics": {"sharpe": 1.5}, "error": None},
            },
        ],
    }

    candidate = service.select_candidate(report)

    assert candidate["window_index"] == 1
    assert candidate["config"] == {"lookback": 20}
    assert candidate["metrics"] == {"sharpe": 1.5}


def test_save_and_load_promotion_round_trip(tmp_path: Path) -> None:
    service = ResearchArtifactService(root=tmp_path)

    promotion = service.save_promotion(
        report_id="wf-1",
        strategy_id="strategy-1",
        strategy_name="example.mean_reversion",
        candidate={
            "kind": "parameter_sweep",
            "result_index": 0,
            "window_index": None,
            "objective": "sharpe",
            "config": {"lookback": 20},
            "metrics": {"sharpe": 1.9},
            "summary": {"start_date": "2026-03-01", "end_date": "2026-03-31"},
        },
        instruments=["AAPL.EQUS"],
        created_by="user-1",
        paper_trading=True,
    )

    loaded = service.load_promotion(promotion["id"])

    assert loaded["strategy_id"] == "strategy-1"
    assert loaded["config"] == {"lookback": 20}
    assert loaded["live_url"] == f"/live?promotion_id={promotion['id']}"


def test_select_candidate_rejects_failed_parameter_sweep_results(tmp_path: Path) -> None:
    service = ResearchArtifactService(root=tmp_path)
    report = {
        "mode": "parameter_sweep",
        "results": [
            {"config": {"lookback": 10}, "metrics": None, "error": "boom"},
        ],
    }

    with pytest.raises(ResearchPromotionError):
        service.select_candidate(report)


def test_select_candidate_rejects_pruned_parameter_sweep_results(tmp_path: Path) -> None:
    service = ResearchArtifactService(root=tmp_path)
    report = {
        "mode": "parameter_sweep",
        "results": [
            {
                "config": {"lookback": 10},
                "metrics": {"sharpe": 3.0},
                "error": None,
                "pruned": True,
                "completed_full_run": False,
            },
        ],
    }

    with pytest.raises(ResearchPromotionError):
        service.select_candidate(report)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))
