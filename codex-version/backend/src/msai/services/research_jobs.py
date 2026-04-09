from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from msai.core.config import settings


class ResearchJobNotFoundError(FileNotFoundError):
    """Raised when a requested research job cannot be found."""


class ResearchJobService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or settings.research_root / "jobs"

    def create_job(
        self,
        *,
        job_type: str,
        strategy_id: str,
        strategy_name: str,
        strategy_path: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = str(uuid4())
        payload = {
            "id": job_id,
            "job_type": job_type,
            "status": "pending",
            "progress": 0,
            "progress_message": "Queued",
            "stage_index": None,
            "stage_count": None,
            "completed_trials": 0,
            "total_trials": None,
            "created_at": _now_iso(),
            "started_at": None,
            "completed_at": None,
            "error_message": None,
            "report_id": None,
            "report_summary": None,
            "queue_name": None,
            "queue_job_id": None,
            "worker_id": None,
            "attempt": 0,
            "heartbeat_at": None,
            "cancel_requested": False,
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "strategy_path": strategy_path,
            "instruments": list(request.get("instruments") or []),
            "objective": request.get("objective"),
            "request": request,
        }
        self._write_json(self._job_path(job_id), payload)
        return payload

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        jobs = [self._read_json(path) for path in sorted(self.root.glob("*.json"))]
        jobs.sort(
            key=lambda job: str(job.get("created_at") or ""),
            reverse=True,
        )
        return jobs[:limit]

    def load_job(self, job_id: str) -> dict[str, Any]:
        path = self._job_path(job_id)
        if not path.exists():
            raise ResearchJobNotFoundError(f"Research job not found: {job_id}")
        return self._read_json(path)

    def mark_running(self, job_id: str, *, worker_id: str | None = None) -> dict[str, Any]:
        payload = self.load_job(job_id)
        attempt = int(payload.get("attempt") or 0) + 1
        return self.update_job(
            job_id,
            status="running",
            progress=5,
            progress_message="Initializing research run",
            started_at=_now_iso(),
            heartbeat_at=_now_iso(),
            worker_id=worker_id,
            attempt=attempt,
            error_message=None,
        )

    def mark_enqueued(
        self,
        job_id: str,
        *,
        queue_name: str,
        queue_job_id: str | None,
    ) -> dict[str, Any]:
        return self.update_job(
            job_id,
            queue_name=queue_name,
            queue_job_id=queue_job_id,
        )

    def mark_completed(
        self,
        job_id: str,
        *,
        report_id: str,
        report_summary: dict[str, Any],
    ) -> dict[str, Any]:
        return self.update_job(
            job_id,
            status="completed",
            progress=100,
            progress_message="Completed",
            completed_at=_now_iso(),
            heartbeat_at=_now_iso(),
            report_id=report_id,
            report_summary=report_summary,
            error_message=None,
        )

    def mark_failed(self, job_id: str, *, error_message: str) -> dict[str, Any]:
        return self.update_job(
            job_id,
            status="failed",
            progress=100,
            progress_message="Failed",
            completed_at=_now_iso(),
            heartbeat_at=_now_iso(),
            error_message=error_message,
        )

    def mark_cancelled(self, job_id: str, *, message: str = "Cancelled") -> dict[str, Any]:
        return self.update_job(
            job_id,
            status="cancelled",
            progress=100,
            progress_message=message,
            completed_at=_now_iso(),
            heartbeat_at=_now_iso(),
            error_message=None,
        )

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        payload = self.load_job(job_id)
        status = str(payload.get("status") or "pending")
        if status in {"completed", "failed", "cancelled"}:
            return payload
        next_status = "cancelling" if status == "running" else status
        next_message = "Cancellation requested" if status == "running" else str(payload.get("progress_message") or "Queued")
        return self.update_job(
            job_id,
            cancel_requested=True,
            status=next_status,
            progress_message=next_message,
            heartbeat_at=_now_iso(),
        )

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"heartbeat_at": _now_iso()}
        if worker_id is not None:
            payload["worker_id"] = worker_id
        payload.update(fields)
        return self.update_job(job_id, **payload)

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        payload = self.load_job(job_id)
        payload.update(fields)
        self._write_json(self._job_path(job_id), payload)
        return payload

    def _job_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

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
