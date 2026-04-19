"""Unit tests for the email alerting service."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from msai.core.logging import setup_logging
from msai.services.alerting import AlertService

if TYPE_CHECKING:
    pass


class TestSendAlert:
    """Tests for :meth:`AlertService.send_alert`."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        """Ensure structlog is configured before each test."""
        setup_logging("development")

    def test_send_alert_logs_warning_without_smtp(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When SMTP is not configured the service should log a warning and return False."""
        # Arrange
        setup_logging("production")  # JSON output for assertion
        service = AlertService()  # No smtp_host

        # Act
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            service.send_alert("Test Subject", "Test body", ["admin@example.com"])
        )

        # Assert
        assert result is False
        captured = capsys.readouterr()
        assert "alert_not_sent_no_smtp" in captured.out

    def test_send_alert_returns_false_without_recipients(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When SMTP is configured but no recipients are provided, return False."""
        # Arrange
        setup_logging("production")
        service = AlertService(smtp_host="smtp.example.com", sender="alerts@example.com")

        # Act
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(
            service.send_alert("Test Subject", "Test body", [])
        )

        # Assert
        assert result is False
        captured = capsys.readouterr()
        assert "alert_not_sent_no_recipients" in captured.out


class TestAlertStrategyError:
    """Tests for :meth:`AlertService.alert_strategy_error`."""

    @pytest.fixture(autouse=True)
    def _setup_logging(self) -> None:
        setup_logging("development")

    def test_alert_strategy_error_calls_send_alert(self) -> None:
        """Verify alert_strategy_error constructs the correct subject and body."""
        # Arrange
        service = AlertService()
        service.send_alert = AsyncMock(return_value=False)

        # Act
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            service.alert_strategy_error("EMA_Cross", "Division by zero")
        )

        # Assert
        service.send_alert.assert_called_once()
        call_args = service.send_alert.call_args
        assert call_args[0][0] == "Strategy Error: EMA_Cross"
        assert "Division by zero" in call_args[0][1]

    def test_alert_daily_loss_calls_send_alert(self) -> None:
        """Verify alert_daily_loss formats P&L values correctly."""
        # Arrange
        service = AlertService()
        service.send_alert = AsyncMock(return_value=False)

        # Act
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            service.alert_daily_loss(-5000.50, -3000.00)
        )

        # Assert
        service.send_alert.assert_called_once()
        call_args = service.send_alert.call_args
        assert "Daily Loss Threshold Breached" in call_args[0][0]
        assert "-5,000.50" in call_args[0][1]
        assert "-3,000.00" in call_args[0][1]

    def test_alert_ib_disconnect_calls_send_alert(self) -> None:
        """Verify alert_ib_disconnect sends the correct alert."""
        # Arrange
        service = AlertService()
        service.send_alert = AsyncMock(return_value=False)

        # Act
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            service.alert_ib_disconnect()
        )

        # Assert
        service.send_alert.assert_called_once()
        call_args = service.send_alert.call_args
        assert "IB Gateway Disconnected" in call_args[0][0]

    def test_alert_system_down_includes_service_name(self) -> None:
        """Verify alert_system_down includes the service name in subject and body."""
        # Arrange
        service = AlertService()
        service.send_alert = AsyncMock(return_value=False)

        # Act
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            service.alert_system_down("postgres")
        )

        # Assert
        service.send_alert.assert_called_once()
        call_args = service.send_alert.call_args
        assert "Service Down: postgres" in call_args[0][0]
        assert "postgres" in call_args[0][1]
