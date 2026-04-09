from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from msai.core.config import settings
from msai.schemas.research import ResearchReportSummary


class ResearchArtifactNotFoundError(FileNotFoundError):
    """Raised when a requested research artifact cannot be found."""


class ResearchPromotionError(ValueError):
    """Raised when a research report cannot be promoted safely."""


class ResearchArtifactService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.research_root
        self.promotions_root = self.root / "promotions"

    def list_reports(self, *, limit: int = 100) -> list[ResearchReportSummary]:
        files = sorted(self._report_files(), key=lambda path: path.stat().st_mtime, reverse=True)
        return [self._build_summary(path) for path in files[:limit]]

    def load_report(self, report_id: str) -> dict[str, Any]:
        path = self._report_path(report_id)
        return self._read_json(path)

    def load_report_detail(self, report_id: str) -> tuple[dict[str, Any], ResearchReportSummary]:
        path = self._report_path(report_id)
        report = self._read_json(path)
        return report, self._summarize(report_id, report)

    def compare_reports(self, report_ids: list[str]) -> list[tuple[dict[str, Any], ResearchReportSummary]]:
        return [self.load_report_detail(report_id) for report_id in report_ids]

    def select_candidate(
        self,
        report: dict[str, Any],
        *,
        result_index: int | None = None,
        window_index: int | None = None,
    ) -> dict[str, Any]:
        mode = str(report.get("mode") or "")
        if mode == "parameter_sweep":
            return self._select_sweep_candidate(report, result_index=result_index)
        if mode == "walk_forward":
            return self._select_walk_forward_candidate(report, window_index=window_index)
        raise ResearchPromotionError(f"Unsupported report mode: {mode or 'unknown'}")

    def save_promotion(
        self,
        *,
        report_id: str,
        strategy_id: str,
        strategy_name: str,
        candidate: dict[str, Any],
        instruments: list[str],
        created_by: str | None,
        paper_trading: bool,
    ) -> dict[str, Any]:
        promotion_id = str(uuid4())
        payload = {
            "id": promotion_id,
            "report_id": report_id,
            "created_at": datetime.now(UTC).isoformat(),
            "created_by": created_by,
            "paper_trading": paper_trading,
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "instruments": instruments,
            "config": candidate["config"],
            "selection": {
                "kind": candidate["kind"],
                "objective": candidate.get("objective"),
                "result_index": candidate.get("result_index"),
                "window_index": candidate.get("window_index"),
                "metrics": candidate.get("metrics"),
                "summary": candidate.get("summary"),
            },
            "live_url": f"/live?promotion_id={promotion_id}",
        }
        self.promotions_root.mkdir(parents=True, exist_ok=True)
        (self.promotions_root / f"{promotion_id}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True)
        )
        return payload

    def load_promotion(self, promotion_id: str) -> dict[str, Any]:
        path = self.promotions_root / f"{promotion_id}.json"
        if not path.exists():
            raise ResearchArtifactNotFoundError(f"Promotion not found: {promotion_id}")
        return self._read_json(path)

    def _report_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [path for path in self.root.glob("*.json") if path.is_file()]

    def _report_path(self, report_id: str) -> Path:
        path = self.root / f"{report_id}.json"
        if not path.exists():
            raise ResearchArtifactNotFoundError(f"Research report not found: {report_id}")
        return path

    def _build_summary(self, path: Path) -> ResearchReportSummary:
        return self._summarize(path.stem, self._read_json(path))

    def _summarize(self, report_id: str, report: dict[str, Any]) -> ResearchReportSummary:
        try:
            candidate = self.select_candidate(report)
        except ResearchPromotionError:
            candidate = {}
        instruments = [str(value) for value in report.get("instruments", []) if value]
        strategy_path = report.get("strategy_path")
        return ResearchReportSummary(
            id=report_id,
            mode=str(report.get("mode") or "unknown"),
            generated_at=_to_text(report.get("generated_at")),
            strategy_path=_to_text(strategy_path),
            strategy_name=_strategy_name(strategy_path),
            instruments=instruments,
            objective=_to_text(report.get("objective")),
            start_date=_to_text(report.get("start_date")),
            end_date=_to_text(report.get("end_date")),
            summary=_jsonable(report.get("summary") or {}),
            best_config=_jsonable(candidate.get("config")),
            best_metrics=_jsonable(candidate.get("metrics")),
            candidate_count=_candidate_count(report),
        )

    def _select_sweep_candidate(
        self,
        report: dict[str, Any],
        *,
        result_index: int | None = None,
    ) -> dict[str, Any]:
        results = list(report.get("results") or [])
        if not results:
            raise ResearchPromotionError("Parameter sweep report does not contain any results")

        if result_index is None:
            selected_index = next(
                (
                    index
                    for index, candidate in enumerate(results)
                    if candidate.get("error") is None
                    and not bool(candidate.get("pruned"))
                    and bool(candidate.get("completed_full_run", True))
                ),
                0,
            )
        else:
            selected_index = result_index
        if selected_index < 0 or selected_index >= len(results):
            raise ResearchPromotionError("Parameter sweep result index is out of range")

        candidate = results[selected_index]
        if candidate.get("error") is not None:
            raise ResearchPromotionError("Selected parameter sweep result failed and cannot be promoted")
        if bool(candidate.get("pruned")) or not bool(candidate.get("completed_full_run", True)):
            raise ResearchPromotionError("Selected parameter sweep result was pruned and cannot be promoted")

        return {
            "kind": "parameter_sweep",
            "result_index": selected_index,
            "window_index": None,
            "objective": report.get("objective"),
            "config": _jsonable(candidate.get("config") or {}),
            "metrics": _jsonable(candidate.get("metrics") or {}),
            "summary": {
                "start_date": candidate.get("start_date"),
                "end_date": candidate.get("end_date"),
            },
        }

    def _select_walk_forward_candidate(
        self,
        report: dict[str, Any],
        *,
        window_index: int | None = None,
    ) -> dict[str, Any]:
        windows = list(report.get("windows") or [])
        if not windows:
            raise ResearchPromotionError("Walk-forward report does not contain any windows")

        if window_index is None:
            objective = str(report.get("objective") or "sharpe")
            successful_windows = [
                (index, window)
                for index, window in enumerate(windows)
                if window.get("test_result") and window["test_result"].get("error") is None
            ]
            if successful_windows:
                window_index, selected_window = max(
                    successful_windows,
                    key=lambda item: _metric_value(item[1]["test_result"], objective),
                )
            else:
                window_index, selected_window = 0, windows[0]
        else:
            if window_index < 0 or window_index >= len(windows):
                raise ResearchPromotionError("Walk-forward window index is out of range")
            selected_window = windows[window_index]

        best_train_result = selected_window.get("best_train_result")
        if not best_train_result or best_train_result.get("error") is not None:
            raise ResearchPromotionError("Selected walk-forward window does not have a promotable config")

        test_result = selected_window.get("test_result")
        return {
            "kind": "walk_forward",
            "result_index": None,
            "window_index": window_index,
            "objective": report.get("objective"),
            "config": _jsonable(best_train_result.get("config") or {}),
            "metrics": _jsonable((test_result or {}).get("metrics") or best_train_result.get("metrics") or {}),
            "summary": {
                "train_start": selected_window.get("train_start"),
                "train_end": selected_window.get("train_end"),
                "test_start": selected_window.get("test_start"),
                "test_end": selected_window.get("test_end"),
            },
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text())


def _candidate_count(report: dict[str, Any]) -> int:
    if report.get("mode") == "walk_forward":
        return len(report.get("windows") or [])
    return len(report.get("results") or [])


def _strategy_name(strategy_path: object) -> str | None:
    if not strategy_path:
        return None
    path = Path(str(strategy_path))
    parts = list(path.parts)
    if "strategies" in parts:
        index = parts.index("strategies")
        return ".".join(Path(*parts[index + 1 :]).with_suffix("").parts)
    return path.with_suffix("").name


def _to_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metric_value(result: dict[str, Any], metric: str) -> float:
    metrics = result.get("metrics") or {}
    value = metrics.get(metric)
    if value is None:
        return float("-inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value))
