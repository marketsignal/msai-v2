"""FastAPI lifespan must boot cleanly even when IB Gateway is down.

This file pins the Revision R1 contract from the plan
(``docs/plans/2026-05-16-ui-completeness.md``, section
"Revision R1 (supersedes T2)"). The original T2 outline awaited
``IB.connectAsync`` inside :meth:`IBAccountSnapshot.start`; that
caused the FastAPI app to fail boot when the IB Gateway was
unreachable.

The corrected pattern:

1. :meth:`IBAccountSnapshot.start` is synchronous and only spawns the
   refresh task. The IB connection is established **inside** the loop
   body.
2. Any exception in the loop body (``ConnectionRefusedError``,
   ``TimeoutError``, etc.) is logged and absorbed — the task keeps
   running and retries on the next tick.
3. ``/health`` keeps returning 200 (it's a separate liveness probe).
4. ``/api/v1/account/summary`` returns the zero-summary shape until a
   refresh succeeds; it never returns 5xx.

The tests below simulate IB Gateway being down by injecting a fake
``IB`` whose ``connectAsync`` always raises ``TimeoutError`` or
``ConnectionRefusedError``.

Like ``test_ib_account_snapshot.py`` we build a minimal FastAPI app
with just the account router (and a fake ``/health``) rather than
importing ``msai.main`` — T4 has deleted modules that ``main.py``
still imports until T4b lands. httpx ``ASGITransport`` does **not**
trigger ASGI lifespan automatically, so the test fixture drives the
``lifespan.startup`` / ``lifespan.shutdown`` events manually through
the ASGI interface.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from msai.api.account import (
    router,
    start_ib_account_snapshot,
    start_ib_probe_task,
    stop_ib_account_snapshot,
    stop_ib_probe_task,
)
from msai.core.auth import get_current_user
from msai.services import ib_account_snapshot as snap_module

_MOCK_CLAIMS: dict[str, Any] = {
    "sub": "test-user",
    "preferred_username": "test@example.com",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _failing_fake_ib(exc: BaseException) -> MagicMock:
    """Build a MagicMock ``IB`` whose ``connectAsync`` always raises ``exc``."""

    fake = MagicMock(name="FailingFakeIB")

    async def _connect(*args: Any, **kwargs: Any) -> None:
        _ = (args, kwargs)
        raise exc

    fake.connectAsync = AsyncMock(side_effect=_connect)
    fake.disconnect = MagicMock(return_value=None)
    return fake


async def _wait_for_connect_attempt(
    snapshot: snap_module.IBAccountSnapshot, max_iters: int = 100
) -> None:
    """Poll until the snapshot's fake IB has been asked to connect at least once.

    Used as a synchronisation primitive — the refresh loop fires
    asynchronously; tests need to know the first tick happened before
    inspecting state. Times out after ``max_iters * 20ms`` (=2 s) to
    keep CI runs bounded.
    """
    fake = snapshot._ib  # noqa: SLF001
    assert fake is not None
    for _ in range(max_iters):
        if fake.connectAsync.await_count >= 1:
            return
        await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_snapshot_singleton() -> Generator[None, None, None]:
    """Reset the module-level snapshot singleton before and after each test."""
    snap_module.reset_snapshot()
    yield
    snap_module.reset_snapshot()


@pytest.fixture
def app_with_lifespan(
    reset_snapshot_singleton: None,
) -> Generator[FastAPI, None, None]:
    """FastAPI app with the account router + a lifespan mirroring ``msai.main``.

    The lifespan calls :func:`start_ib_probe_task` and
    :func:`start_ib_account_snapshot`. Tests inject a failing fake IB
    onto :func:`get_snapshot` **before** entering the lifespan; the
    test asserts the lifespan still enters without raising.

    A bare ``/health`` route is added so the test can confirm the app
    boots — the real ``main.py`` exposes one at the same path.
    """
    _ = reset_snapshot_singleton

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await start_ib_probe_task()
        await start_ib_account_snapshot()
        try:
            yield
        finally:
            await stop_ib_account_snapshot()
            await stop_ib_probe_task()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _MOCK_CLAIMS

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "healthy"}

    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client_with_lifespan(
    app_with_lifespan: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async client that drives ASGI lifespan startup + shutdown around the test.

    httpx ``ASGITransport`` handles HTTP traffic but does NOT trigger
    the lifespan span automatically. We manually push the
    ``lifespan.startup``/``lifespan.shutdown`` events through the ASGI
    interface so the snapshot + probe tasks actually launch.
    """
    receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await receive_queue.get()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {"type": "lifespan", "app": app_with_lifespan}

    # Kick off the ASGI lifespan coroutine. The FastAPI ASGI callable
    # is sync `__call__` -> coroutine; awaiting it here would block, so
    # we let it run as a background task.
    lifespan_task = asyncio.create_task(app_with_lifespan(scope, receive, send))

    # Send startup event and wait for the app to acknowledge.
    await receive_queue.put({"type": "lifespan.startup"})
    for _ in range(100):
        await asyncio.sleep(0.02)
        if any(m["type"].startswith("lifespan.startup") for m in sent):
            break

    startup_msgs = [m for m in sent if m["type"].startswith("lifespan.startup")]
    assert startup_msgs, "ASGI lifespan never produced a startup message"
    assert startup_msgs[0]["type"] == "lifespan.startup.complete", (
        f"Lifespan startup failed even though IB is down: {startup_msgs[0]!r}"
    )

    transport = httpx.ASGITransport(app=app_with_lifespan)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
    finally:
        # Drive shutdown.
        await receive_queue.put({"type": "lifespan.shutdown"})
        for _ in range(100):
            await asyncio.sleep(0.02)
            if any(m["type"].startswith("lifespan.shutdown") for m in sent):
                break
        try:
            await asyncio.wait_for(lifespan_task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            lifespan_task.cancel()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLifespanBootsWhenIbGatewayIsDown:
    """The acceptance criterion for Revision R1 — boot must not block."""

    async def test_startup_completes_when_connect_raises_timeout(
        self,
        client_with_lifespan: httpx.AsyncClient,
    ) -> None:
        # Arrange — pre-attach a fake IB to the (already-created)
        # snapshot singleton. ``get_snapshot()`` was lazy-built during
        # the lifespan startup; the fixture already entered startup
        # before this test body runs, so we hook the IB after the fact.
        snapshot = snap_module.get_snapshot()
        snapshot._ib = _failing_fake_ib(TimeoutError("IB Gateway timeout"))  # noqa: SLF001

        # Reset the connected flag so the loop tries to reconnect on
        # the next tick — without this, an earlier connect failure
        # could leave us racing the loop's sleep.
        snapshot._connected = False  # noqa: SLF001

        # Act — hit /health to confirm the app is up.
        response = await client_with_lifespan.get("/health")

        # Assert
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

        # The snapshot's refresh loop should have attempted to connect
        # at least once and stayed disconnected.
        await _wait_for_connect_attempt(snapshot)
        assert snapshot.is_connected is False

    async def test_startup_completes_when_connect_raises_connection_refused(
        self,
        client_with_lifespan: httpx.AsyncClient,
    ) -> None:
        snapshot = snap_module.get_snapshot()
        snapshot._ib = _failing_fake_ib(  # noqa: SLF001
            ConnectionRefusedError("IB Gateway refused connection")
        )
        snapshot._connected = False  # noqa: SLF001

        response = await client_with_lifespan.get("/health")
        assert response.status_code == 200

    async def test_account_summary_returns_503_when_ib_down_on_cold_start(
        self,
        client_with_lifespan: httpx.AsyncClient,
    ) -> None:
        """Iter-3 SF P1: on cold start (no successful refresh yet) the
        handler returns 503, not a zero-summary 200. Returning $0.00 on
        cold start was indistinguishable from a real empty account and
        lied to the dashboard. The 503 surfaces the gateway outage so
        TanStack Query routes through its error path."""
        snapshot = snap_module.get_snapshot()
        snapshot._ib = _failing_fake_ib(TimeoutError("IB Gateway timeout"))  # noqa: SLF001
        snapshot._connected = False  # noqa: SLF001
        # Critical: do NOT seed _last_refresh_success_at — this is the
        # cold-start path that the iter-3 change protects.

        await _wait_for_connect_attempt(snapshot)

        response = await client_with_lifespan.get("/api/v1/account/summary")
        assert response.status_code == 503
        body = response.json()
        assert "IB Gateway unreachable" in body["detail"]

    async def test_account_portfolio_returns_503_when_ib_down_on_cold_start(
        self,
        client_with_lifespan: httpx.AsyncClient,
    ) -> None:
        """Iter-3 SF P1: same cold-start guard for portfolio — empty list
        at boot is indistinguishable from "no positions"; 503 surfaces
        the outage honestly."""
        snapshot = snap_module.get_snapshot()
        snapshot._ib = _failing_fake_ib(TimeoutError("IB Gateway timeout"))  # noqa: SLF001
        snapshot._connected = False  # noqa: SLF001
        await _wait_for_connect_attempt(snapshot)

        response = await client_with_lifespan.get("/api/v1/account/portfolio")
        assert response.status_code == 503
        body = response.json()
        assert "IB Gateway unreachable" in body["detail"]


class TestStartIsSynchronous:
    """Anti-regression — :meth:`IBAccountSnapshot.start` must NOT await
    ``connectAsync``. The original T2 outline did, and Codex F1 flagged
    it. This test catches the regression by binding a slow ``connect``
    and asserting ``start()`` returns synchronously, *before* the
    connect coroutine resolves."""

    async def test_start_returns_before_connect_completes(
        self, reset_snapshot_singleton: None
    ) -> None:
        _ = reset_snapshot_singleton
        connect_entered = asyncio.Event()
        connect_complete = asyncio.Event()
        connect_call_count = [0]

        async def _slow_connect(*args: Any, **kwargs: Any) -> None:
            _ = (args, kwargs)
            connect_call_count[0] += 1
            connect_entered.set()
            await connect_complete.wait()

        fake_ib = MagicMock(name="SlowIB")
        fake_ib.connectAsync = AsyncMock(side_effect=_slow_connect)
        fake_ib.accountSummaryAsync = AsyncMock(return_value=[])
        fake_ib.portfolio = MagicMock(return_value=[])
        fake_ib.disconnect = MagicMock(return_value=None)

        snapshot = snap_module.IBAccountSnapshot(host="ib-gateway", port=4002)
        snapshot._ib = fake_ib  # noqa: SLF001

        # ``start()`` is synchronous — it spawns the task and returns
        # immediately. If it were ``async def start: await connect(...)``
        # we would never reach the next line because connect_complete
        # is never set.
        snapshot.start()
        assert snapshot.refresh_task is not None
        assert snapshot.refresh_task.done() is False

        # Wait until the refresh loop has actually entered _slow_connect.
        # The await_count on AsyncMock only increments AFTER the awaited
        # coroutine returns — we use a sentinel event to know the loop
        # is currently inside connectAsync.
        await asyncio.wait_for(connect_entered.wait(), timeout=2.0)

        # We are now mid-connect (still awaiting connect_complete).
        # is_connected stays False until the connect resolves AND
        # summary+portfolio fetches succeed.
        assert connect_call_count[0] == 1
        assert snapshot.is_connected is False

        # Release the slow connect so the refresh loop can drain, then
        # cancel the task and disconnect. Without this the test would
        # deadlock on ``stop()``'s ``await self._refresh_task``.
        connect_complete.set()
        await snapshot.stop()
        assert snapshot.refresh_task is None
