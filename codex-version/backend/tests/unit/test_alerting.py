from __future__ import annotations

from pathlib import Path

from msai.services.alerting import AlertingService


def test_alerting_service_persists_and_limits_recent_alerts(tmp_path: Path) -> None:
    service = AlertingService(path=tmp_path / "alerts.json")

    service.send_alert("error", "Research job failed", "Walk-forward worker exited with code 1.")
    service.send_recovery("Research job recovered", "Retry completed successfully.")

    alerts = service.list_alerts(limit=1)

    assert len(alerts) == 1
    assert alerts[0]["title"] == "Research job recovered"
    assert alerts[0]["type"] == "recovery"
