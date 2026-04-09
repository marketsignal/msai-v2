from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from msai.core.config import settings
from msai.core.logging import get_logger

logger = get_logger("alerting")


class AlertingService:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.alerts_path

    def send_alert(self, level: str, title: str, message: str) -> None:
        self._write_event("alert", level=level, title=title, message=message)
        logger.warning("alert", level=level, title=title, message=message)

    def send_recovery(self, title: str, message: str) -> None:
        self._write_event("recovery", level="info", title=title, message=message)
        logger.info("alert_recovery", title=title, message=message)

    def list_alerts(self, *, limit: int = 50) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return []
        alerts = payload.get("alerts") or []
        return list(alerts)[:limit]

    def _write_event(self, event_type: str, *, level: str, title: str, message: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = {"alerts": []}
        if self.path.exists():
            try:
                current = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                current = {"alerts": []}
        alerts = list(current.get("alerts") or [])
        alerts.insert(
            0,
            {
                "type": event_type,
                "level": level,
                "title": title,
                "message": message,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        self.path.write_text(json.dumps({"alerts": alerts[:200]}, indent=2, sort_keys=True))


alerting_service = AlertingService()
