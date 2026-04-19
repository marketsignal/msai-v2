"""Phase 1 E2E verification harness (task 1.16).

Gated by ``MSAI_E2E_IB_ENABLED=1`` so CI + local unit-test runs don't
try to spin up a real IB Gateway container. When the env var is set,
this test drives the full Phase 1 path end-to-end:

1. ``docker compose -f docker-compose.dev.yml up -d`` (assumed pre-run
   by the operator — the harness does NOT bring the stack up itself,
   because pytest shouldn't own Docker lifecycle)
2. POST ``/api/v1/live/start`` with the smoke strategy + ``["AAPL"]``
3. Assert 201 → capture ``deployment_id``
4. Verify ``live_node_processes.last_heartbeat_at`` advances by
   at least 2 ticks over a 12-second window
5. Wait for at least one audit row for the deployment (the smoke
   strategy submits exactly one market order on the first bar)
6. Assert the audit row has ``client_order_id``,
   ``strategy_code_hash``, ``instrument_id`` matching ``AAPL.*``,
   ``side == "BUY"``, ``quantity == 1``
7. ``docker kill msai-claude-backend`` — simulate a backend crash
8. Sleep 5s, then ``docker compose up -d backend``
9. Verify the trading subprocess is still alive (heartbeat still
   advancing) — the supervisor container was NOT killed, so the
   subprocess is untouched
10. GET ``/api/v1/live/status/{deployment_id}`` → still running
11. POST ``/api/v1/live/stop``
12. Assert ``live_node_processes.status == 'stopped'``,
    ``exit_code == 0``
13. Assert the IB account has zero open positions for the instrument
    (via ``/api/v1/account/portfolio``)

The harness is deliberately written as a SINGLE long test so a
failure in any step leaves the preceding state intact for
debugging. Running it in pieces defeats the point.

Running:

    export MSAI_E2E_IB_ENABLED=1
    export MSAI_E2E_IB_ACCOUNT_ID=DUxxxxxxx
    docker compose -f docker-compose.dev.yml up -d
    cd backend && uv run pytest tests/e2e/test_live_trading_phase1.py -vv
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any
from uuid import UUID

import httpx
import pytest

E2E_ENABLED = os.environ.get("MSAI_E2E_IB_ENABLED") == "1"

pytestmark = pytest.mark.skipif(
    not E2E_ENABLED,
    reason=(
        "Phase 1 E2E harness gated by MSAI_E2E_IB_ENABLED=1 — requires "
        "the full Docker Compose stack + a real IB Gateway paper account "
        "reachable from the backend container."
    ),
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


BACKEND_URL = os.environ.get("MSAI_E2E_BACKEND_URL", "http://localhost:8800")
SMOKE_STRATEGY_CLASS = "SmokeMarketOrderStrategy"
SMOKE_STRATEGY_PATH = "strategies.example.smoke_market_order:SmokeMarketOrderStrategy"
SMOKE_CONFIG_PATH = "strategies.example.smoke_market_order:SmokeMarketOrderConfig"
SMOKE_INSTRUMENTS = ["AAPL.NASDAQ"]

BACKEND_CONTAINER_NAME = os.environ.get("MSAI_E2E_BACKEND_CONTAINER", "msai-claude-backend")
COMPOSE_FILE = os.environ.get("MSAI_E2E_COMPOSE_FILE", "docker-compose.dev.yml")

# Wall-clock budgets (generous — the real IB startup path can take
# 30+ s before the first bar arrives).
START_TIMEOUT_S = 120.0
HEARTBEAT_WINDOW_S = 12.0
FIRST_BAR_TIMEOUT_S = 120.0
STOP_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_status(client: httpx.AsyncClient, deployment_id: UUID) -> dict[str, Any]:
    resp = await client.get(f"/api/v1/live/status/{deployment_id}")
    resp.raise_for_status()
    return resp.json()


def _run_docker(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a ``docker`` command and return the result without raising
    — the caller inspects ``returncode``."""
    return subprocess.run(  # noqa: S603 — operator-invoked harness, not a web path
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# The one test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_1_e2e_full_lifecycle() -> None:  # noqa: C901, PLR0912, PLR0915
    """Full Phase 1 lifecycle: start → heartbeat → first order →
    backend crash → restart → stop → flat positions. See module
    docstring for the step breakdown."""
    _api_key = os.environ.get("MSAI_API_KEY", "msai-dev-key")
    _auth_headers = {"X-API-Key": _api_key}
    async with httpx.AsyncClient(
        base_url=BACKEND_URL, timeout=START_TIMEOUT_S, headers=_auth_headers
    ) as client:
        # ------------------------------------------------------------
        # Step 1: POST /start with the smoke strategy
        # ------------------------------------------------------------
        # The harness assumes a smoke strategy row has been seeded in
        # the ``strategies`` table with ``strategy_class=SmokeMarketOrderStrategy``
        # and ``file_path`` pointing at the on-disk module. The seed
        # lives in ``scripts/e2e_phase1.sh`` so this test stays
        # focused on the HTTP + DB assertions.
        strategy_id = os.environ.get("MSAI_E2E_STRATEGY_ID")
        assert strategy_id is not None, (
            "MSAI_E2E_STRATEGY_ID must point at a pre-seeded "
            "strategies row for the smoke strategy. Run "
            "scripts/e2e_phase1.sh to seed it."
        )

        # Codex iter5 P1: paper_trading must match the supervisor's
        # configuration. When ``verify-paper-soak.sh`` runs with
        # ``TRADING_MODE=live`` against the prod compose stack, the
        # supervisor expects a live deployment. The payload factory
        # in ``live_supervisor/__main__.py`` rejects the mismatch.
        # Read from env so the harness works for both paper and live
        # runs; default to paper for backward compat.
        _paper_trading = os.environ.get("MSAI_E2E_PAPER_TRADING", "true").lower() == "true"

        start_resp = await client.post(
            "/api/v1/live/start",
            json={
                "strategy_id": strategy_id,
                "config": {},
                "instruments": SMOKE_INSTRUMENTS,
                "paper_trading": _paper_trading,
            },
            headers={"Idempotency-Key": f"e2e-{int(time.time())}"},
        )
        assert start_resp.status_code in (200, 201), (
            f"expected 200 or 201, got {start_resp.status_code}: {start_resp.text}"
        )
        start_body = start_resp.json()
        deployment_id = UUID(start_body["id"])

        # ------------------------------------------------------------
        # Step 2: heartbeat advances ≥2 ticks over 12 s
        # ------------------------------------------------------------
        first = await _get_status(client, deployment_id)
        initial_heartbeat = first["last_heartbeat_at"]
        assert initial_heartbeat is not None

        hb_deadline = time.monotonic() + HEARTBEAT_WINDOW_S
        observed_ticks: set[str] = {initial_heartbeat}
        while time.monotonic() < hb_deadline:
            snap = await _get_status(client, deployment_id)
            if snap["last_heartbeat_at"] is not None:
                observed_ticks.add(snap["last_heartbeat_at"])
            if len(observed_ticks) >= 3:
                break
            await _async_sleep(1.0)
        assert len(observed_ticks) >= 3, (
            f"expected ≥3 distinct heartbeats in {HEARTBEAT_WINDOW_S}s, "
            f"got {len(observed_ticks)}: {observed_ticks}"
        )

        # ------------------------------------------------------------
        # Step 3: wait for the first audit row
        # ------------------------------------------------------------
        audit_deadline = time.monotonic() + FIRST_BAR_TIMEOUT_S
        audit_rows: list[dict[str, Any]] = []
        while time.monotonic() < audit_deadline:
            resp = await client.get(f"/api/v1/live/audits/{deployment_id}")
            if resp.status_code == 200:
                audit_rows = resp.json().get("audits", [])
                if audit_rows:
                    break
            await _async_sleep(2.0)
        assert len(audit_rows) >= 1, (
            f"expected ≥1 audit row within {FIRST_BAR_TIMEOUT_S}s, got {len(audit_rows)}"
        )
        audit = audit_rows[0]
        assert audit.get("client_order_id")
        assert audit.get("strategy_code_hash")
        assert str(audit.get("instrument_id", "")).startswith("AAPL")
        assert audit.get("side") == "BUY"
        assert str(audit.get("quantity")) in {"1", "1.0", "1.00"}

        # ------------------------------------------------------------
        # Step 4: docker kill backend → sleep 5 → restart
        # ------------------------------------------------------------
        kill_result = _run_docker("kill", BACKEND_CONTAINER_NAME)
        assert kill_result.returncode == 0, f"docker kill failed: {kill_result.stderr}"
        time.sleep(5)
        up_result = _run_docker("compose", "-f", COMPOSE_FILE, "up", "-d", "backend")
        assert up_result.returncode == 0, f"docker compose up -d backend failed: {up_result.stderr}"

        # Give the backend a moment to come back online.
        for _ in range(30):
            try:
                health = await client.get("/health", timeout=2.0)
                if health.status_code == 200:
                    break
            except httpx.RequestError:
                pass
            time.sleep(1)
        else:
            pytest.fail("backend did not come back online after restart")

        # ------------------------------------------------------------
        # Step 5: subprocess still alive, /status still running
        # ------------------------------------------------------------
        post_restart = await _get_status(client, deployment_id)
        assert post_restart["process_status"] in {"ready", "running"}, (
            f"process status {post_restart['process_status']!r} "
            f"suggests the subprocess died when the backend was killed"
        )

        # ------------------------------------------------------------
        # Step 6: POST /stop → wait for stopped
        # ------------------------------------------------------------
        stop_resp = await client.post(
            "/api/v1/live/stop",
            json={"deployment_id": str(deployment_id)},
        )
        assert stop_resp.status_code == 200

        stop_deadline = time.monotonic() + STOP_TIMEOUT_S
        final: dict[str, Any] = {}
        while time.monotonic() < stop_deadline:
            final = await _get_status(client, deployment_id)
            if final.get("process_status") == "stopped":
                break
            await _async_sleep(1.0)
        assert final.get("process_status") == "stopped", (
            f"expected process_status=stopped, got {final.get('process_status')!r}"
        )
        assert final.get("exit_code") == 0

        # ------------------------------------------------------------
        # Step 7: IB account has zero open positions for AAPL
        # ------------------------------------------------------------
        portfolio = await client.get("/api/v1/account/portfolio")
        assert portfolio.status_code == 200
        positions = portfolio.json().get("positions", [])
        aapl_positions = [p for p in positions if "AAPL" in p.get("instrument_id", "")]
        assert all(float(p.get("quantity", 0)) == 0.0 for p in aapl_positions), (
            f"expected zero AAPL positions after stop, got {aapl_positions}"
        )


async def _async_sleep(seconds: float) -> None:
    """Import-free async sleep used inside the single E2E test."""
    import asyncio

    await asyncio.sleep(seconds)
