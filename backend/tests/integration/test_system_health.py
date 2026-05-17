"""Integration tests for ``GET /api/v1/system/health``.

These tests stand up a minimal FastAPI app containing only the system
router so they remain runnable even while T4 is being integrated.  Once
T4b wires ``system_router`` into ``msai.main``, the same suite can be
re-pointed at the real app without changes to assertion logic.

The endpoint requires authentication; we override
:func:`msai.core.auth.get_current_user` with a constant claims dict.
Each test patches the appropriate probe function on
``msai.api.system`` to simulate healthy / unhealthy subsystem states —
we never actually take Postgres or Redis down.

Per the "no cheating in ARRANGE/VERIFY" rule (`testing.md`), this
file does not write to the DB or Redis directly; the patches only
substitute the subsystem-status return value the API would have
produced. Real end-to-end coverage lives in the verify-e2e use case
``UC-4`` (Phase 5.4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator  # noqa: TC003 — runtime types for fixtures
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from msai.api import system as system_module
from msai.api.system import SubsystemStatus, router
from msai.core.auth import get_current_user

_MOCK_CLAIMS: dict[str, Any] = {
    "sub": "test-user",
    "preferred_username": "test@example.com",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_system_router() -> Generator[FastAPI, None, None]:
    """Minimal FastAPI app containing only the system router.

    Decoupled from ``msai.main`` so the test runs even before T4b wires
    the router into the production app. The dependency override on
    ``get_current_user`` matches the pattern used by ``tests/conftest.py``
    so behaviour is identical to the real app.
    """
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _MOCK_CLAIMS
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app_with_system_router: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Async test client wired to the minimal system-router app."""
    transport = httpx.ASGITransport(app=app_with_system_router)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _healthy_status(**extra: Any) -> SubsystemStatus:
    """Build a healthy status snapshot with arbitrary subsystem-specific extras."""
    return SubsystemStatus(
        status="healthy",
        last_checked="2026-05-16T00:00:00Z",
        detail=None,
        **extra,
    )


def _unhealthy_status(detail: str, **extra: Any) -> SubsystemStatus:
    """Build an unhealthy status snapshot with the given detail string."""
    return SubsystemStatus(
        status="unhealthy",
        last_checked="2026-05-16T00:00:00Z",
        detail=detail,
        **extra,
    )


def _patch_all_probes(
    *,
    db: SubsystemStatus | None = None,
    redis: SubsystemStatus | None = None,
    workers: SubsystemStatus | None = None,
    ib_gateway: SubsystemStatus | None = None,
    parquet: SubsystemStatus | None = None,
) -> list[Any]:
    """Build a list of ``patch.object`` context managers covering every probe.

    Defaults all unspecified probes to healthy. Callers compose the list
    with ``contextlib.ExitStack`` so a single ``with`` block keeps a flat
    indentation regardless of how many probes are being overridden.
    """
    return [
        patch.object(
            system_module,
            "_probe_db",
            new=AsyncMock(return_value=db or _healthy_status()),
        ),
        patch.object(
            system_module,
            "_probe_redis",
            new=AsyncMock(return_value=redis or _healthy_status()),
        ),
        patch.object(
            system_module,
            "_probe_workers",
            new=AsyncMock(return_value=workers or _healthy_status(queue_depth=0)),
        ),
        patch.object(
            system_module,
            "_probe_ib_gateway",
            return_value=ib_gateway or _healthy_status(),
        ),
        patch.object(
            system_module,
            "_probe_parquet",
            return_value=parquet or _healthy_status(total_files=0, total_bytes=0),
        ),
    ]


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestSystemHealthHappyPath:
    """All subsystems report healthy → endpoint returns 200 + every key."""

    async def test_returns_200_with_documented_shape(self, client: httpx.AsyncClient) -> None:
        # Arrange — patch every probe to a healthy state.
        with ExitStack() as stack:
            for p in _patch_all_probes(
                workers=_healthy_status(queue_depth=0),
                parquet=_healthy_status(total_files=12, total_bytes=4096),
            ):
                stack.enter_context(p)
            # Act
            response = await client.get("/api/v1/system/health")

        # Assert
        assert response.status_code == 200
        body = response.json()

        # Top-level shape
        assert set(body.keys()) == {"subsystems", "version", "commit_sha", "uptime_seconds"}
        assert isinstance(body["version"], str) and body["version"]
        assert isinstance(body["commit_sha"], str) and body["commit_sha"]
        assert isinstance(body["uptime_seconds"], int)
        assert body["uptime_seconds"] >= 0

        # All six subsystems present
        assert set(body["subsystems"].keys()) == {
            "api",
            "db",
            "redis",
            "ib_gateway",
            "workers",
            "parquet",
        }

        # Every subsystem has the required keys
        for name, sub in body["subsystems"].items():
            assert "status" in sub, f"{name} missing status"
            assert "last_checked" in sub, f"{name} missing last_checked"
            assert sub["status"] in {"healthy", "unhealthy", "unknown"}, (
                f"{name} has bogus status {sub['status']!r}"
            )

        # All probes returned healthy
        assert all(sub["status"] == "healthy" for sub in body["subsystems"].values())

        # Subsystem-specific extras present
        assert body["subsystems"]["workers"]["queue_depth"] == 0
        assert body["subsystems"]["parquet"]["total_files"] == 12
        assert body["subsystems"]["parquet"]["total_bytes"] == 4096

    async def test_api_subsystem_is_always_healthy(self, client: httpx.AsyncClient) -> None:
        """The ``api`` subsystem reflects "is FastAPI itself answering" —
        if the request reaches this handler, by construction the API
        is up. No external probe required."""
        with ExitStack() as stack:
            for p in _patch_all_probes():
                stack.enter_context(p)
            response = await client.get("/api/v1/system/health")

        assert response.json()["subsystems"]["api"]["status"] == "healthy"


# ---------------------------------------------------------------------------
# Tests — failure cases
# ---------------------------------------------------------------------------


class TestSystemHealthSubsystemFailures:
    """When DB / Redis / IB Gateway probes report unhealthy, the endpoint
    still returns 200 — the failure is reflected *inside* the body so
    the dashboard can render per-subsystem red indicators rather than
    hitting a generic 5xx."""

    async def test_db_unhealthy_renders_in_body_not_status_code(
        self, client: httpx.AsyncClient
    ) -> None:
        with ExitStack() as stack:
            for p in _patch_all_probes(db=_unhealthy_status("connection refused")):
                stack.enter_context(p)
            response = await client.get("/api/v1/system/health")

        assert response.status_code == 200
        body = response.json()
        assert body["subsystems"]["db"]["status"] == "unhealthy"
        assert body["subsystems"]["db"]["detail"] == "connection refused"
        # Other subsystems remain healthy
        assert body["subsystems"]["redis"]["status"] == "healthy"

    async def test_redis_unhealthy_renders_in_body(self, client: httpx.AsyncClient) -> None:
        with ExitStack() as stack:
            for p in _patch_all_probes(redis=_unhealthy_status("timeout after 0.5s")):
                stack.enter_context(p)
            response = await client.get("/api/v1/system/health")

        assert response.status_code == 200
        body = response.json()
        assert body["subsystems"]["redis"]["status"] == "unhealthy"
        assert "timeout" in body["subsystems"]["redis"]["detail"]

    async def test_ib_gateway_unhealthy_renders_in_body(self, client: httpx.AsyncClient) -> None:
        with ExitStack() as stack:
            for p in _patch_all_probes(ib_gateway=_unhealthy_status("consecutive_failures=3")):
                stack.enter_context(p)
            response = await client.get("/api/v1/system/health")

        assert response.status_code == 200
        body = response.json()
        assert body["subsystems"]["ib_gateway"]["status"] == "unhealthy"
        assert body["subsystems"]["ib_gateway"]["detail"] == "consecutive_failures=3"

    async def test_ib_gateway_unknown_when_probe_loop_not_started(
        self, client: httpx.AsyncClient
    ) -> None:
        """If the probe task hasn't started yet the gateway state is
        genuinely unknown, not unhealthy. The UI renders this with a
        neutral indicator rather than a red alarm."""
        unknown = SubsystemStatus(
            status="unknown",
            last_checked="2026-05-16T00:00:00Z",
            detail="probe loop not running",
        )
        with ExitStack() as stack:
            for p in _patch_all_probes(ib_gateway=unknown):
                stack.enter_context(p)
            response = await client.get("/api/v1/system/health")

        assert response.status_code == 200
        assert response.json()["subsystems"]["ib_gateway"]["status"] == "unknown"

    async def test_multiple_subsystems_unhealthy_independent(
        self, client: httpx.AsyncClient
    ) -> None:
        """DB + Redis simultaneously down — the endpoint must still
        return a complete payload so the dashboard can show *both*
        problems rather than the first one masking the second."""
        with ExitStack() as stack:
            for p in _patch_all_probes(
                db=_unhealthy_status("db down"),
                redis=_unhealthy_status("redis down"),
            ):
                stack.enter_context(p)
            response = await client.get("/api/v1/system/health")

        assert response.status_code == 200
        body = response.json()
        assert body["subsystems"]["db"]["status"] == "unhealthy"
        assert body["subsystems"]["redis"]["status"] == "unhealthy"
        assert body["subsystems"]["db"]["detail"] == "db down"
        assert body["subsystems"]["redis"]["detail"] == "redis down"


# ---------------------------------------------------------------------------
# Tests — auth required
# ---------------------------------------------------------------------------


class TestSystemHealthAuth:
    """The endpoint must require authentication; an unauthenticated
    request returns 401/403 (the per-test override above lets every
    other test pass)."""

    async def test_unauthenticated_returns_401(self, app_with_system_router: FastAPI) -> None:
        # Drop the override for this single test
        app_with_system_router.dependency_overrides.pop(get_current_user, None)
        transport = httpx.ASGITransport(app=app_with_system_router)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            response = await c.get("/api/v1/system/health")

        # FastAPI's HTTPBearer returns 403 by default on missing creds;
        # the auth.get_current_user dep raises 401 once it has a token
        # to reject. Accept both — what matters is "not 200".
        assert response.status_code in {401, 403}
