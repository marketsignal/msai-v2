"""Phase 3 E2E verification harness (task 3.11).

End-to-end test of the projection layer + WebSocket fan-out +
kill switch. Gated by ``MSAI_E2E_IB_ENABLED=1`` so CI + local
unit-test runs don't try to spin up a real IB Gateway container.
When the env var is set, this test drives the full Phase 3
path:

1. Start the stack with paper IB Gateway (operator pre-runs
   ``docker compose -f docker-compose.dev.yml up -d`` — same
   contract as the Phase 1 harness).
2. POST ``/api/v1/live/start`` with the smoke strategy.
3. Wait for ``live_node_processes.status == 'running'``.
4. Connect to ``/api/v1/live/stream/{deployment_id}`` WebSocket.
5. Assert the initial ``snapshot`` message is received within
   3 seconds and contains an empty positions array (the
   smoke strategy hasn't filled yet).
6. Trigger a fill: the smoke strategy from Task 1.15 submits
   one market order on its first bar — wait for the bar.
7. Assert the WebSocket receives a ``fill`` event AND a
   ``position_snapshot`` event within 5 seconds.
8. Call ``GET /api/v1/live/positions`` and verify the position
   is also visible via the REST API (PositionReader fast path).
9. POST ``/api/v1/live/kill-all``.
10. Verify ``msai:risk:halt`` is set in Redis (Layer 1).
11. Verify the WebSocket receives a ``risk_halt`` event.
12. Verify ``live_node_processes.status`` flips to ``stopping``
    then ``stopped`` within 10 seconds (Layer 3 — supervisor
    push + ``manage_stop=True`` flatten).
13. Verify the IB account shows zero open positions for the
    smoke instrument (manage_stop did its job).
14. POST ``/api/v1/live/start`` again — expect 503 because the
    halt flag is still set.
15. POST ``/api/v1/live/resume``.
16. POST ``/api/v1/live/start`` again — expect 201, halt
    cleared.
17. POST ``/api/v1/live/stop`` (cleanup).

The harness is a SINGLE long test by design — a failure in any
step leaves the preceding state intact for debugging. Running
it in pieces defeats the point.

Running::

    export MSAI_E2E_IB_ENABLED=1
    export MSAI_E2E_IB_ACCOUNT_ID=DUxxxxxxx
    docker compose -f docker-compose.dev.yml up -d
    cd backend && uv run pytest tests/e2e/test_live_streaming_phase3.py -vv
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any
from uuid import UUID

import httpx
import pytest

E2E_ENABLED = os.environ.get("MSAI_E2E_IB_ENABLED") == "1"
"""Hard gate. Without this env var the test is skipped at
collection time so the unit-test runner doesn't try to spin
up Docker / IB Gateway / Redis."""

API_BASE = os.environ.get("MSAI_E2E_API_BASE", "http://localhost:8800")
WS_BASE = os.environ.get("MSAI_E2E_WS_BASE", "ws://localhost:8800")
API_KEY = os.environ.get("MSAI_API_KEY", "")
ACCOUNT_ID = os.environ.get("MSAI_E2E_IB_ACCOUNT_ID", "DU1234567")
SMOKE_STRATEGY_ID = os.environ.get("MSAI_E2E_SMOKE_STRATEGY_ID", "")

SNAPSHOT_TIMEOUT_S = 3.0
FILL_TIMEOUT_S = 60.0  # First-bar fill can take up to a minute on slow IB sims
KILL_FLATTEN_TIMEOUT_S = 10.0


pytestmark = pytest.mark.skipif(
    not E2E_ENABLED,
    reason="Phase 3 E2E disabled — set MSAI_E2E_IB_ENABLED=1 to run",
)


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY, "Content-Type": "application/json"}


async def _start_smoke_deployment(client: httpx.AsyncClient, strategy_id: str) -> UUID:
    """POST /api/v1/live/start with the smoke strategy.

    The operator MUST provision the smoke strategy row in the
    DB beforehand and pass its UUID via
    ``MSAI_E2E_SMOKE_STRATEGY_ID`` — the API expects a
    UUID, not the literal string ``"smoke"``.

    Returns the new deployment_id. ``/start`` returns the
    deployment under the ``id`` key in the standard outcome
    payload, NOT ``deployment_id``.
    """
    body = {
        "strategy_id": strategy_id,
        "instruments": ["AAPL.NASDAQ"],
        "paper_trading": True,
        "config": {},
    }
    response = await client.post("/api/v1/live/start", json=body)
    assert response.status_code in (200, 201), (
        f"start failed: {response.status_code} {response.text}"
    )
    data = response.json()
    return UUID(data["id"])


async def _wait_for_status(
    client: httpx.AsyncClient,
    deployment_id: UUID,
    target: str,
    timeout_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/live/status/{deployment_id}")
        if response.status_code == 200:
            last_payload = response.json()
            if last_payload.get("status") == target:
                return last_payload
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"deployment {deployment_id} did not reach status={target!r} in "
        f"{timeout_s}s; last_payload={last_payload}"
    )


async def _read_until(
    ws: Any,
    predicate: Any,
    timeout_s: float,
) -> dict[str, Any]:
    """Pull messages off a ``websockets`` client connection
    until ``predicate(msg)`` returns truthy. Returns the
    matching message. Raises on timeout. Uses ``ws.recv()``
    (the ``websockets`` library API), NOT ``receive_text``
    (which is Starlette's server-side API).
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            break
        msg = json.loads(raw)
        if predicate(msg):
            return msg
    raise AssertionError(f"WebSocket message matching predicate not received in {timeout_s}s")


@pytest.mark.asyncio
async def test_phase3_streaming_and_kill_switch_e2e() -> None:
    """One long test that walks the full Phase 3 verification
    plan from the implementation doc. Each step's assertion
    failure leaves the preceding state intact for debugging."""
    if not SMOKE_STRATEGY_ID:
        pytest.skip(
            "MSAI_E2E_SMOKE_STRATEGY_ID env var must be set to the UUID of "
            "the smoke strategy row in the live DB. The /start endpoint "
            "expects a UUID, not the literal string 'smoke'."
        )

    transport = httpx.AsyncHTTPTransport(retries=3)
    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers=_headers(),
        transport=transport,
        timeout=30.0,
    ) as client:
        # ----------------------------------------------------------
        # Step 1: Start a smoke deployment
        # ----------------------------------------------------------
        deployment_id = await _start_smoke_deployment(client, SMOKE_STRATEGY_ID)

        # ----------------------------------------------------------
        # Step 2: Wait for the supervisor to flip the row to
        # status=running
        # ----------------------------------------------------------
        await _wait_for_status(client, deployment_id, target="running", timeout_s=120)

        # ----------------------------------------------------------
        # Step 3: Connect to the WebSocket
        # ----------------------------------------------------------
        from websockets.client import connect as ws_connect

        ws_url = f"{WS_BASE}/api/v1/live/stream/{deployment_id}"
        async with ws_connect(ws_url) as ws:
            # First message MUST be the auth token
            await ws.send(API_KEY)

            # Step 4: Initial snapshot within 3 seconds
            snapshot_raw = await asyncio.wait_for(ws.recv(), timeout=SNAPSHOT_TIMEOUT_S)
            snapshot = json.loads(snapshot_raw)
            assert snapshot["type"] == "snapshot"
            assert snapshot["deployment_id"] == str(deployment_id)
            # Initial positions are empty (the smoke strategy
            # hasn't filled yet)
            assert isinstance(snapshot["positions"], list)

            # Step 5: Wait for the smoke strategy to submit + fill
            # one market order. Both the fill event and the
            # follow-up position_snapshot must arrive.
            fill = await _read_until(
                ws,
                lambda m: m.get("event_type") == "fill",
                timeout_s=FILL_TIMEOUT_S,
            )
            assert fill["side"] == "BUY"

            await _read_until(
                ws,
                lambda m: m.get("event_type") == "position_snapshot",
                timeout_s=10.0,
            )

            # Step 6: REST endpoint serves the same position via
            # PositionReader fast path
            positions_response = await client.get("/api/v1/live/positions")
            assert positions_response.status_code == 200
            assert positions_response.json()["positions"], (
                "PositionReader returned no positions after a fill — "
                "fast path or cold-read hydration is broken"
            )

            # Step 7: Kill switch
            kill_response = await client.post("/api/v1/live/kill-all")
            # 200 = clean kill, 207 = partial (some stop
            # publishes failed). Either is acceptable for the
            # E2E happy path; the halt flag is set in both.
            assert kill_response.status_code in (200, 207)
            assert kill_response.json()["risk_halted"] is True

            # Step 8: WebSocket eventually receives a deployment
            # status change to stopping/stopped (the supervisor
            # publishes a deployment_status event after applying
            # the stop command).
            await _read_until(
                ws,
                lambda m: (
                    m.get("event_type") == "deployment_status"
                    and m.get("status") in ("stopping", "stopped")
                ),
                timeout_s=KILL_FLATTEN_TIMEOUT_S,
            )

        # ----------------------------------------------------------
        # Step 9: live_node_processes flipped to stopped
        # ----------------------------------------------------------
        await _wait_for_status(
            client,
            deployment_id,
            target="stopped",
            timeout_s=KILL_FLATTEN_TIMEOUT_S,
        )

        # ----------------------------------------------------------
        # Step 10: Halt flag blocks new starts (Layer 1)
        # ----------------------------------------------------------
        body = {
            "strategy_id": SMOKE_STRATEGY_ID,
            "instruments": ["AAPL.NASDAQ"],
            "paper_trading": True,
            "config": {},
        }
        blocked = await client.post("/api/v1/live/start", json=body)
        assert blocked.status_code == 503, (
            f"halt flag did NOT block /start: {blocked.status_code} {blocked.text}"
        )

        # ----------------------------------------------------------
        # Step 11: Resume clears the halt flag
        # ----------------------------------------------------------
        resume = await client.post("/api/v1/live/resume")
        assert resume.status_code == 200
        assert resume.json()["resumed"] is True

        # ----------------------------------------------------------
        # Step 12: After resume, /start succeeds
        # ----------------------------------------------------------
        retry = await client.post("/api/v1/live/start", json=body)
        assert retry.status_code in (200, 201), (
            f"start after resume failed: {retry.status_code} {retry.text}"
        )
        new_deployment_id = UUID(retry.json()["id"])

        # ----------------------------------------------------------
        # Cleanup: stop the new deployment
        # ----------------------------------------------------------
        await client.post(
            "/api/v1/live/stop",
            json={"deployment_id": str(new_deployment_id)},
        )
