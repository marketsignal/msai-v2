from __future__ import annotations

import os
import time
from contextlib import suppress
from typing import Any

import httpx
import pytest

RUN_PAPER_E2E = os.getenv("RUN_PAPER_E2E") == "1"
BASE_URL = os.getenv("PAPER_E2E_BASE_URL", "http://127.0.0.1:8400")
API_KEY = os.getenv("PAPER_E2E_API_KEY", os.getenv("MSAI_API_KEY", "msai-dev-key"))
DEFAULT_INSTRUMENT = os.getenv("PAPER_E2E_INSTRUMENT", "AAPL.XNAS")
POLL_TIMEOUT_SECONDS = float(os.getenv("PAPER_E2E_TIMEOUT_SECONDS", "90"))

pytestmark = pytest.mark.skipif(
    not RUN_PAPER_E2E,
    reason="set RUN_PAPER_E2E=1 to run broker-connected paper E2E smoke tests",
)


def test_ib_paper_live_start_and_stop_smoke() -> None:
    client = httpx.Client(base_url=BASE_URL, headers={"X-API-Key": API_KEY}, timeout=20.0)
    deployment_id: str | None = None

    try:
        ready = client.get("/ready")
        ready.raise_for_status()

        _kill_all_and_reset(client)

        strategies = client.get("/api/v1/strategies/")
        strategies.raise_for_status()
        strategy_rows = strategies.json()
        assert strategy_rows, "no strategies were registered in the backend"

        strategy = next(
            (row for row in strategy_rows if row.get("name") == "example.ema_cross"),
            strategy_rows[0],
        )
        strategy_id = str(strategy["id"])

        detail = client.get(f"/api/v1/strategies/{strategy_id}")
        detail.raise_for_status()
        default_config = detail.json().get("default_config") or {}

        start_response = client.post(
            "/api/v1/live/start",
            json={
                "strategy_id": strategy_id,
                "config": default_config,
                "instruments": [DEFAULT_INSTRUMENT],
                "paper_trading": True,
            },
        )
        start_response.raise_for_status()
        deployment_id = str(start_response.json()["deployment_id"])

        running_row = _wait_for_deployment(
            client,
            deployment_id,
            allowed_statuses={"running", "starting"},
        )
        assert running_row["paper_trading"] is True
        assert running_row["process_alive"] is True

        risk_status = client.get("/api/v1/live/risk-status")
        risk_status.raise_for_status()
        assert "halted" in risk_status.json()

        positions = client.get("/api/v1/live/positions")
        positions.raise_for_status()
        assert isinstance(positions.json(), list)

        stop_response = client.post(
            "/api/v1/live/stop",
            json={"deployment_id": deployment_id},
        )
        stop_response.raise_for_status()
        _wait_for_stop(client, deployment_id)
    finally:
        if deployment_id is not None:
            with suppress(httpx.HTTPError):
                client.post("/api/v1/live/stop", json={"deployment_id": deployment_id})
        _kill_all_and_reset(client)
        client.close()


def _wait_for_deployment(
    client: httpx.Client,
    deployment_id: str,
    *,
    allowed_statuses: set[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last_row: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get("/api/v1/live/status")
        response.raise_for_status()
        rows = response.json()
        last_row = next((row for row in rows if str(row.get("id")) == deployment_id), None)
        if last_row and str(last_row.get("status")) in allowed_statuses:
            return last_row
        time.sleep(2.0)

    raise AssertionError(f"deployment {deployment_id} did not reach one of {sorted(allowed_statuses)}: {last_row}")


def _wait_for_stop(client: httpx.Client, deployment_id: str) -> None:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last_row: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get("/api/v1/live/status")
        response.raise_for_status()
        rows = response.json()
        last_row = next((row for row in rows if str(row.get("id")) == deployment_id), None)
        if last_row is None or str(last_row.get("status")) == "stopped":
            return
        time.sleep(2.0)

    raise AssertionError(f"deployment {deployment_id} did not stop cleanly: {last_row}")


def _kill_all_and_reset(client: httpx.Client) -> None:
    kill = client.post("/api/v1/live/kill-all")
    if kill.status_code not in {200, 400}:
        kill.raise_for_status()

    reset = client.post("/api/v1/live/reset-halt")
    if reset.status_code not in {200, 400}:
        reset.raise_for_status()
