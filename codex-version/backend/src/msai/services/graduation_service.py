from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from msai.core.config import settings

GRADUATION_STAGES = {
    "paper_candidate",
    "paper_running",
    "paper_review",
    "live_candidate",
    "live_running",
    "paused",
    "archived",
}


class GraduationCandidateNotFoundError(FileNotFoundError):
    """Raised when a graduation candidate cannot be found."""


class GraduationStageError(ValueError):
    """Raised when an invalid graduation stage transition is requested."""


class GraduationService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.graduation_root

    def list_candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        rows = [self._read_json(path) for path in sorted(self.root.glob("*.json"))]
        rows.sort(key=lambda row: str(row.get("updated_at") or row.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def load_candidate(self, candidate_id: str) -> dict[str, Any]:
        path = self._path(candidate_id)
        if not path.exists():
            raise GraduationCandidateNotFoundError(f"Graduation candidate not found: {candidate_id}")
        return self._read_json(path)

    def create_candidate(
        self,
        *,
        promotion: dict[str, Any],
        strategy_path: str,
        created_by: str | None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        candidate_id = str(uuid4())
        now = _now_iso()
        payload = {
            "id": candidate_id,
            "promotion_id": str(promotion["id"]),
            "report_id": str(promotion["report_id"]),
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
            "stage": "paper_candidate",
            "notes": notes,
            "strategy_id": str(promotion["strategy_id"]),
            "strategy_name": str(promotion["strategy_name"]),
            "strategy_path": strategy_path,
            "instruments": list(promotion.get("instruments") or []),
            "config": dict(promotion.get("config") or {}),
            "selection": dict(promotion.get("selection") or {}),
            "paper_trading": bool(promotion.get("paper_trading", True)),
            "live_url": f"/live?candidate_id={candidate_id}",
            "portfolio_url": f"/portfolio?candidate_id={candidate_id}",
        }
        self._write_json(self._path(candidate_id), payload)
        return payload

    def update_stage(
        self,
        candidate_id: str,
        *,
        stage: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if stage not in GRADUATION_STAGES:
            raise GraduationStageError(f"Unsupported graduation stage: {stage}")
        candidate = self.load_candidate(candidate_id)
        candidate["stage"] = stage
        candidate["updated_at"] = _now_iso()
        if notes is not None:
            candidate["notes"] = notes
        self._write_json(self._path(candidate_id), candidate)
        return candidate

    def _path(self, candidate_id: str) -> Path:
        return self.root / f"{candidate_id}.json"

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        temp_path.replace(path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text())


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
