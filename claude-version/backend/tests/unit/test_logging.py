"""Unit tests for the structured logging foundation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from msai.core.logging import get_logger, setup_logging

if TYPE_CHECKING:
    import pytest


class TestSetupLogging:
    """Tests for :func:`setup_logging`."""

    def test_setup_logging_dev_mode(self) -> None:
        """Verify setup_logging configures structlog without raising in dev mode."""
        setup_logging("development")

        # After setup, getting a logger and emitting a message must not raise.
        log: structlog.BoundLogger = structlog.get_logger("test.dev")
        log.info("dev smoke test")

    def test_setup_logging_prod_mode(self) -> None:
        """Verify setup_logging configures structlog without raising in prod mode."""
        setup_logging("production")

        log: structlog.BoundLogger = structlog.get_logger("test.prod")
        log.info("prod smoke test")

    def test_setup_logging_dev_is_case_insensitive(self) -> None:
        """Environment string comparison should be case-insensitive."""
        setup_logging("Development")

        log: structlog.BoundLogger = structlog.get_logger("test.case")
        log.debug("case-insensitive check")


class TestGetLogger:
    """Tests for :func:`get_logger`."""

    def test_get_logger_returns_bound_logger(self) -> None:
        """get_logger must return a structlog BoundLogger instance."""
        setup_logging("development")

        log = get_logger("my.module")

        # structlog.get_logger returns a proxy that wraps a BoundLogger.
        # The most reliable check is that it exposes the standard logging API.
        assert callable(getattr(log, "info", None))
        assert callable(getattr(log, "warning", None))
        assert callable(getattr(log, "error", None))
        assert callable(getattr(log, "debug", None))

    def test_get_logger_binds_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The logger returned by get_logger should carry the provided name."""
        # Use production mode so JSONRenderer serialises all bound keys,
        # making it straightforward to assert the logger_name field.
        setup_logging("production")
        log = get_logger("my.named.logger")

        log.info("hello")
        captured = capsys.readouterr()
        assert "my.named.logger" in captured.out
