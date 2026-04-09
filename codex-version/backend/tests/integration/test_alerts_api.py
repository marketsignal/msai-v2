from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from msai.api import alerts as alerts_api
from msai.core.config import settings
from msai.main import app
from msai.services.alerting import AlertingService


@pytest.fixture(autouse=True)
def _reset_alerts_app_state() -> None:
    app.dependency_overrides.clear()


def test_alerts_endpoint_returns_recent_persisted_alerts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "msai_api_key", "msai-test-key")
    service = AlertingService(path=tmp_path / "alerts.json")
    service.send_alert("error", "Daily ingest failed", "Databento request timed out.")
    service.send_recovery("Daily ingest recovered", "Retry succeeded.")
    monkeypatch.setattr(alerts_api, "alerting_service", service)

    with TestClient(app) as client:
        response = client.get("/api/v1/alerts/?limit=1", headers={"X-API-Key": "msai-test-key"})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["alerts"]) == 1
    assert payload["alerts"][0]["title"] == "Daily ingest recovered"
    assert payload["alerts"][0]["type"] == "recovery"
