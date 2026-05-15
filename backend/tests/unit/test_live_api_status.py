"""Unit tests for ``GET /api/v1/live/status`` halt-flag source.

T0a (live-deployment-workflow-ui-cli): the ``risk_halted`` field on
``GET /api/v1/live/status`` must read from the *persistent* Redis halt
flag (``msai:risk:halt``) — the same key that ``POST /api/v1/live/kill-all``
writes and ``POST /api/v1/live/resume`` deletes — and NOT from the
process-local ``_risk_engine.is_halted`` attribute, which does not
survive a backend restart and does not propagate across worker
replicas.

The UI's Resume button relies on this: after a kill-all the operator
reloads the page (or another browser tab opens), and the Resume button
must still be visible because the halt is persistent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from msai.api.live_deps import get_command_bus
from msai.core.database import get_db
from msai.main import app
from msai.services.live_command_bus import LiveCommandBus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# ---------------------------------------------------------------------------
# Fixtures (mirror test_live_api.py shape so the override surface matches)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db() -> AsyncMock:
    """AsyncSession stub returning empty result sets by default."""
    session = AsyncMock(spec=AsyncSession)

    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = []
    mock_result.scalars.return_value = mock_scalars
    mock_result.scalar_one.return_value = 0
    mock_result.scalar_one_or_none.return_value = None

    session.execute.return_value = mock_result
    return session


@pytest.fixture
def mock_command_bus() -> MagicMock:
    """Stub LiveCommandBus whose ``_redis.exists`` controls the halt flag."""
    bus = MagicMock(spec=LiveCommandBus)
    fake_redis = MagicMock()
    # Tests override `exists` to control the halt-flag state.
    fake_redis.exists = AsyncMock(return_value=0)
    # Other Redis methods that the bus surface might touch incidentally —
    # provide harmless defaults so unrelated paths don't blow up.
    fake_redis.set = AsyncMock(return_value=True)
    fake_redis.delete = AsyncMock(return_value=0)
    fake_redis.get = AsyncMock(return_value=None)
    bus._redis = fake_redis  # noqa: SLF001
    return bus


@pytest.fixture
def client(
    mock_db: AsyncMock,
    mock_command_bus: MagicMock,
) -> httpx.AsyncClient:
    """Async test client with DB + command-bus dependencies overridden."""

    async def _override_get_db() -> AsyncGenerator[AsyncMock, None]:
        yield mock_db

    async def _override_get_bus() -> LiveCommandBus:
        return mock_command_bus

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_command_bus] = _override_get_bus

    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://testserver")  # type: ignore[misc]
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_command_bus, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLiveStatusReadsPersistentHaltFlag:
    """``GET /api/v1/live/status`` must read ``risk_halted`` from Redis."""

    @pytest.mark.asyncio
    async def test_risk_halted_true_when_halt_key_present_in_redis(
        self,
        client: httpx.AsyncClient,
        mock_command_bus: MagicMock,
    ) -> None:
        """When ``msai:risk:halt`` exists in Redis, response is ``risk_halted: true``.

        This simulates the post-kill-all state: the persistent halt flag
        is set; the UI calling ``/status`` (on first load OR on reload)
        must see it and render the Resume button.
        """
        # Arrange — halt flag is set in Redis.
        mock_command_bus._redis.exists = AsyncMock(return_value=1)  # noqa: SLF001

        # Act
        async with client as ac:
            response = await ac.get("/api/v1/live/status")

        # Assert
        assert response.status_code == 200
        assert response.json()["risk_halted"] is True
        mock_command_bus._redis.exists.assert_awaited_with("msai:risk:halt")  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_risk_halted_false_when_halt_key_absent_in_redis(
        self,
        client: httpx.AsyncClient,
        mock_command_bus: MagicMock,
    ) -> None:
        """When ``msai:risk:halt`` is absent, response is ``risk_halted: false``.

        This simulates post-resume (or never-killed) state.
        """
        # Arrange — halt flag is NOT set.
        mock_command_bus._redis.exists = AsyncMock(return_value=0)  # noqa: SLF001

        # Act
        async with client as ac:
            response = await ac.get("/api/v1/live/status")

        # Assert
        assert response.status_code == 200
        assert response.json()["risk_halted"] is False
        mock_command_bus._redis.exists.assert_awaited_with("msai:risk:halt")  # noqa: SLF001
