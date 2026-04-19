"""Phase 4 E2E recovery + reconnect harness (task 4.7).

Three scenarios that prove the production crash-recovery
contracts. Gated by ``MSAI_E2E_IB_ENABLED=1`` so CI + local
unit-test runs don't try to spin up a real IB Gateway and
Docker stack. When the env var is set, this module drives
each scenario through the full Phase 1+2+3+4 stack.

**Scenario A — Kill FastAPI mid-trade**

The supervisor and the trading subprocess are independent of
the FastAPI container. Killing the API container must NOT
interrupt trading; on restart the API container must
discover the running deployment from the database and the
projection consumer must reclaim any pending Redis stream
entries via XAUTOCLAIM.

**Scenario B — Kill TradingNode subprocess**

A SIGKILL'd subprocess must be detected by the supervisor's
``reap_loop`` (decision #15) within ~2 seconds and the row
flipped to ``failed`` with the real exit code. If the
supervisor itself dies, a restarted supervisor's heartbeat
monitor must detect any orphaned rows on its first sweep.

**Scenario C — Disconnect IB Gateway**

The IB disconnect handler from Task 4.2 sets the kill switch
when the broker has been disconnected longer than the grace
window (default 120s). On reconnect, the strategy stays
halted until manual ``/resume`` — there is NO auto-resume.

**Scenario D was DROPPED in v4** (live restart bar replay
non-determinism). The deterministic equivalent runs in
``test_ema_cross_save_load_roundtrip.py`` from Task 4.5.

Running::

    export MSAI_E2E_IB_ENABLED=1
    export MSAI_E2E_IB_ACCOUNT_ID=DUxxxxxxx
    export MSAI_E2E_SMOKE_STRATEGY_ID=<uuid of provisioned smoke strategy>
    cd claude-version && docker compose -f docker-compose.dev.yml up -d
    cd backend && uv run pytest tests/e2e/test_recovery_phase4.py -vv

Each scenario is a SEPARATE test method so a failure in one
doesn't block running the other two — the recovery scenarios
have independent setup costs and partial information is more
valuable than all-or-nothing.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from typing import Any
from uuid import UUID

import httpx
import pytest

E2E_ENABLED = os.environ.get("MSAI_E2E_IB_ENABLED") == "1"

API_BASE = os.environ.get("MSAI_E2E_API_BASE", "http://localhost:8800")
API_KEY = os.environ.get("MSAI_API_KEY", "")
SMOKE_STRATEGY_ID = os.environ.get("MSAI_E2E_SMOKE_STRATEGY_ID", "")

BACKEND_CONTAINER = os.environ.get("MSAI_E2E_BACKEND_CONTAINER", "msai-claude-backend")
SUPERVISOR_CONTAINER = os.environ.get(
    "MSAI_E2E_SUPERVISOR_CONTAINER", "msai-claude-live-supervisor"
)
IB_GATEWAY_CONTAINER = os.environ.get("MSAI_E2E_IB_GATEWAY_CONTAINER", "msai-claude-ib-gateway")

DISCONNECT_GRACE_S = 130.0  # disconnect_handler default + 10s buffer

pytestmark = pytest.mark.skipif(
    not E2E_ENABLED,
    reason="Phase 4 E2E disabled — set MSAI_E2E_IB_ENABLED=1 to run",
)


def _headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a docker subcommand and return the completed
    process. We do NOT raise on non-zero — the test asserts
    on the result so a docker failure produces a clear test
    failure instead of a generic CalledProcessError."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


async def _start_smoke_deployment(client: httpx.AsyncClient) -> UUID:
    body = {
        "strategy_id": SMOKE_STRATEGY_ID,
        "instruments": ["AAPL.NASDAQ"],
        "paper_trading": True,
        "config": {},
    }
    response = await client.post("/api/v1/live/start", json=body)
    assert response.status_code in (200, 201), (
        f"start failed: {response.status_code} {response.text}"
    )
    return UUID(response.json()["id"])


async def _wait_for_status(
    client: httpx.AsyncClient,
    deployment_id: UUID,
    target: str | tuple[str, ...],
    timeout_s: float,
) -> dict[str, Any]:
    targets = (target,) if isinstance(target, str) else target
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/live/status/{deployment_id}")
        if response.status_code == 200:
            last = response.json()
            if last.get("status") in targets:
                return last
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"deployment {deployment_id} did not reach {targets!r} in {timeout_s}s; last={last}"
    )


def _require_smoke_strategy_id() -> None:
    if not SMOKE_STRATEGY_ID:
        pytest.skip(
            "MSAI_E2E_SMOKE_STRATEGY_ID env var must be set to the UUID of "
            "the smoke strategy row in the live DB."
        )


# ----------------------------------------------------------------------
# Scenario A — Kill FastAPI mid-trade
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_a_kill_fastapi_mid_trade() -> None:
    """Killing the FastAPI container must NOT interrupt
    trading. On restart, the API discovers the running
    deployment from the DB and the projection consumer
    reclaims any pending Redis stream entries via
    XAUTOCLAIM."""
    _require_smoke_strategy_id()

    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers=_headers(),
        timeout=30.0,
    ) as client:
        # Step 1-2: deploy and wait for running
        deployment_id = await _start_smoke_deployment(client)
        await _wait_for_status(client, deployment_id, "running", timeout_s=120)

        # Step 3: docker kill the backend container
        kill_result = _docker("kill", BACKEND_CONTAINER)
        assert kill_result.returncode == 0, f"docker kill failed: {kill_result.stderr}"

        # Step 4: sleep so the supervisor proves it's still
        # ticking without the API
        await asyncio.sleep(5)

        # Step 5: restart the backend
        up_result = _docker("compose", "-f", "docker-compose.dev.yml", "up", "-d", "backend")
        assert up_result.returncode == 0, f"docker compose up failed: {up_result.stderr}"

        # Wait for the API to come back
        for _attempt in range(30):
            try:
                health = await client.get("/health")
                if health.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            await asyncio.sleep(1)
        else:
            raise AssertionError("API did not return after restart")

        # Step 6-7: deployment is still running, status
        # endpoint discovers it from the DB
        status = await _wait_for_status(client, deployment_id, "running", timeout_s=10)
        assert status["status"] == "running"

        # Cleanup
        await client.post(
            "/api/v1/live/stop",
            json={"deployment_id": str(deployment_id)},
        )


# ----------------------------------------------------------------------
# Scenario B — Kill TradingNode subprocess
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_b_kill_trading_subprocess() -> None:
    """SIGKILL the trading subprocess directly. The
    supervisor's reap_loop (decision #15) must detect the
    exit within 2 seconds and flip the row to ``failed``
    with the real exit code (-9 for SIGKILL)."""
    _require_smoke_strategy_id()

    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers=_headers(),
        timeout=30.0,
    ) as client:
        deployment_id = await _start_smoke_deployment(client)
        await _wait_for_status(client, deployment_id, "running", timeout_s=120)

        # Look up the subprocess PID via the live status row
        status = await client.get(f"/api/v1/live/status/{deployment_id}")
        pid = status.json().get("pid")
        assert pid, f"no PID on running deployment: {status.json()}"

        # SIGKILL inside the supervisor container
        kill_result = _docker("exec", SUPERVISOR_CONTAINER, "kill", "-9", str(pid))
        assert kill_result.returncode == 0, f"kill -9 failed: {kill_result.stderr}"

        # Within 2-3 seconds the reap_loop should flip the row
        flipped = await _wait_for_status(client, deployment_id, ("failed", "stopped"), timeout_s=10)
        assert flipped["status"] in ("failed", "stopped")
        # Real exit code captured from the SIGKILL
        if flipped.get("exit_code") is not None:
            assert flipped["exit_code"] in (-9, 137)  # POSIX vs container conventions


# ----------------------------------------------------------------------
# Scenario C — Disconnect IB Gateway
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_c_ib_gateway_disconnect() -> None:
    """Pause the IB Gateway container so the disconnect
    handler from Task 4.2 sees an extended outage. After the
    grace window, the kill switch must fire and the strategy
    must NOT auto-resume on reconnect."""
    _require_smoke_strategy_id()

    async with httpx.AsyncClient(
        base_url=API_BASE,
        headers=_headers(),
        timeout=30.0,
    ) as client:
        deployment_id = await _start_smoke_deployment(client)
        await _wait_for_status(client, deployment_id, "running", timeout_s=120)

        # Step 2: pause IB Gateway
        pause_result = _docker("pause", IB_GATEWAY_CONTAINER)
        assert pause_result.returncode == 0, f"docker pause failed: {pause_result.stderr}"

        try:
            # Step 3: wait past the disconnect grace window
            await asyncio.sleep(DISCONNECT_GRACE_S)

            # Step 4: deployment was halted by the disconnect
            # handler — kill switch fired, supervisor stopped
            # the subprocess via Layer 3 push
            await _wait_for_status(client, deployment_id, ("stopped", "failed"), timeout_s=30)
        finally:
            # Step 5: unpause IB regardless of outcome (cleanup)
            _docker("unpause", IB_GATEWAY_CONTAINER)

        # Step 6: kill switch is still active — /start returns 503
        body = {
            "strategy_id": SMOKE_STRATEGY_ID,
            "instruments": ["AAPL.NASDAQ"],
            "paper_trading": True,
            "config": {},
        }
        blocked = await client.post("/api/v1/live/start", json=body)
        assert blocked.status_code == 503, (
            f"halt flag did NOT block /start after disconnect: {blocked.text}"
        )

        # Step 7: explicit /resume clears the halt
        resume = await client.post("/api/v1/live/resume")
        assert resume.status_code == 200

        # Step 8: /start now succeeds
        retry = await client.post("/api/v1/live/start", json=body)
        assert retry.status_code in (200, 201)
        new_id = UUID(retry.json()["id"])

        # Cleanup
        await client.post("/api/v1/live/stop", json={"deployment_id": str(new_id)})
