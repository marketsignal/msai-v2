"""Unit tests for the audit logging module."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from msai.core.audit import log_audit
from msai.core.logging import setup_logging


@pytest.fixture(autouse=True)
def _configure_structlog() -> None:
    """Ensure structlog is configured before each test."""
    setup_logging("development")


def _make_mock_session() -> AsyncMock:
    """Create a minimal mock AsyncSession."""
    return AsyncMock()


class TestLogAudit:
    """Tests for :func:`log_audit`."""

    async def test_log_audit_logs_via_structlog(self) -> None:
        """log_audit should emit a structlog entry with all provided fields."""
        # Arrange
        session = _make_mock_session()
        user_id = uuid4()
        resource_id = uuid4()
        details: dict[str, Any] = {"old_name": "alpha", "new_name": "beta"}

        with patch("msai.core.audit.log") as mock_log:
            # Act
            await log_audit(
                db=session,
                user_id=user_id,
                action="update",
                resource_type="strategy",
                resource_id=resource_id,
                details=details,
            )

            # Assert
            mock_log.info.assert_called_once_with(
                "audit",
                user_id=str(user_id),
                action="update",
                resource_type="strategy",
                resource_id=str(resource_id),
                details=details,
            )

    async def test_log_audit_handles_none_user_id(self) -> None:
        """log_audit must not crash when user_id is None (system action)."""
        # Arrange
        session = _make_mock_session()

        with patch("msai.core.audit.log") as mock_log:
            # Act
            await log_audit(
                db=session,
                user_id=None,
                action="cleanup",
                resource_type="backtest",
            )

            # Assert
            mock_log.info.assert_called_once_with(
                "audit",
                user_id=None,
                action="cleanup",
                resource_type="backtest",
                resource_id=None,
                details=None,
            )

    async def test_log_audit_handles_minimal_arguments(self) -> None:
        """log_audit should work with only the required arguments."""
        # Arrange
        session = _make_mock_session()
        user_id = uuid4()

        with patch("msai.core.audit.log") as mock_log:
            # Act
            await log_audit(
                db=session,
                user_id=user_id,
                action="login",
            )

            # Assert
            mock_log.info.assert_called_once_with(
                "audit",
                user_id=str(user_id),
                action="login",
                resource_type=None,
                resource_id=None,
                details=None,
            )

    async def test_log_audit_does_not_interact_with_database(self) -> None:
        """Until the AuditLog model exists, no database writes should occur."""
        # Arrange
        session = _make_mock_session()

        with patch("msai.core.audit.log"):
            # Act
            await log_audit(
                db=session,
                user_id=uuid4(),
                action="delete",
                resource_type="strategy",
                resource_id=uuid4(),
            )

            # Assert -- session should be untouched
            session.add.assert_not_called()
            session.flush.assert_not_called()
            session.commit.assert_not_called()
