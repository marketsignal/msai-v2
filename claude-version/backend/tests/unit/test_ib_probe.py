"""Unit tests for the IB Gateway health probe."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from msai.core.logging import setup_logging
from msai.services.ib_probe import IBProbe


class TestCheckHealth:
    """Tests for ``IBProbe.check_health``."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        setup_logging("development")

    async def test_check_health_returns_false_on_connection_error(self) -> None:
        """When the gateway is unreachable, check_health returns False."""
        # Arrange
        probe = IBProbe(host="localhost", port=19999, timeout=0.1)

        # Act
        result = await probe.check_health()

        # Assert
        assert result is False
        assert probe.is_healthy is False

    async def test_check_health_returns_true_on_success(self) -> None:
        """When the gateway is reachable, check_health returns True."""
        # Arrange
        probe = IBProbe(host="localhost", port=19999)

        mock_writer = AsyncMock()
        mock_writer.close = lambda: None
        mock_writer.wait_closed = AsyncMock()

        async def _mock_open_connection(
            host: str, port: int
        ) -> tuple[AsyncMock, AsyncMock]:
            return AsyncMock(), mock_writer

        with patch(
            "msai.services.ib_probe.asyncio.open_connection",
            side_effect=_mock_open_connection,
        ):
            # Act
            result = await probe.check_health()

        # Assert
        assert result is True
        assert probe.is_healthy is True

    async def test_consecutive_failures_tracked(self) -> None:
        """Consecutive failures counter increments on each failure."""
        # Arrange
        probe = IBProbe(host="localhost", port=19999, timeout=0.1)
        assert probe.consecutive_failures == 0

        # Act
        await probe.check_health()
        first = probe.consecutive_failures

        await probe.check_health()
        second = probe.consecutive_failures

        # Assert
        assert first == 1
        assert second == 2

    async def test_consecutive_failures_reset_on_success(self) -> None:
        """Failure counter resets to 0 after a successful check."""
        # Arrange
        probe = IBProbe(host="localhost", port=19999, timeout=0.1)

        # Cause two failures
        await probe.check_health()
        await probe.check_health()
        assert probe.consecutive_failures == 2

        mock_writer = AsyncMock()
        mock_writer.close = lambda: None
        mock_writer.wait_closed = AsyncMock()

        async def _mock_open_connection(
            host: str, port: int
        ) -> tuple[AsyncMock, AsyncMock]:
            return AsyncMock(), mock_writer

        with patch(
            "msai.services.ib_probe.asyncio.open_connection",
            side_effect=_mock_open_connection,
        ):
            # Act
            await probe.check_health()

        # Assert
        assert probe.consecutive_failures == 0
        assert probe.is_healthy is True

    async def test_initial_state_is_unhealthy(self) -> None:
        """IBProbe starts in an unhealthy state."""
        # Arrange & Act
        probe = IBProbe()

        # Assert
        assert probe.is_healthy is False
        assert probe.consecutive_failures == 0
