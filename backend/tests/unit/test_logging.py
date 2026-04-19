"""Unit tests for the structured logging foundation."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import structlog

from msai.core.logging import bind_deployment, get_logger, setup_logging

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


class TestBindDeployment:
    """Tests for :func:`bind_deployment` (Phase 1 task 1.3).

    The audit trail and triage flow both need every log line emitted from
    a live deployment subprocess to carry the ``deployment_id``. The
    context manager binds it for the duration of a ``with`` block, then
    restores the prior contextvars on exit so concurrent deployments
    don't pollute each other's log streams.
    """

    def test_bind_deployment_injects_into_log_records(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Inside the ``with`` block, every log call must carry ``deployment_id``."""
        setup_logging("production")
        log = get_logger("test.bind")
        dep_id = UUID("aaaa1111-bbbb-2222-cccc-333333333333")

        with bind_deployment(dep_id):
            log.info("inside-with")

        captured = capsys.readouterr()
        assert "inside-with" in captured.out
        assert dep_id.hex in captured.out

    def test_bind_deployment_restores_prior_context_on_exit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """After the ``with`` block exits, ``deployment_id`` must NOT
        appear on subsequent log lines — otherwise concurrent
        deployments would leak ids into each other's streams."""
        setup_logging("production")
        log = get_logger("test.unbind")
        dep_id = uuid4()

        with bind_deployment(dep_id):
            log.info("during")
        log.info("after")

        captured = capsys.readouterr()
        # First line carries the id; second line does not.
        before, after = captured.out.strip().split("\n")
        assert dep_id.hex in before
        assert dep_id.hex not in after

    def test_bind_deployment_accepts_string(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Strings are accepted (used by subprocesses that already have
        the slug-form id rather than a UUID object)."""
        setup_logging("production")
        log = get_logger("test.string")

        with bind_deployment("MSAI-abcd1234abcd1234"):
            log.info("string-id")

        captured = capsys.readouterr()
        assert "MSAI-abcd1234abcd1234" in captured.out

    def test_bind_deployment_nested_restores_outer(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Nested ``with`` blocks must restore the OUTER deployment_id
        on exit, not clear the field entirely. Useful for code that
        delegates to a sub-helper which itself binds a different id."""
        setup_logging("production")
        log = get_logger("test.nested")
        outer_id = UUID("11111111-1111-1111-1111-111111111111")
        inner_id = UUID("22222222-2222-2222-2222-222222222222")

        with bind_deployment(outer_id):
            log.info("outer-1")
            with bind_deployment(inner_id):
                log.info("inner")
            log.info("outer-2")

        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert outer_id.hex in lines[0]
        assert inner_id.hex in lines[1]
        assert outer_id.hex in lines[2]
        # The inner block should not have leaked the inner id past its scope
        assert inner_id.hex not in lines[2]

    def test_bind_deployment_raises_on_exception_still_restores(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If the body of the ``with`` block raises, the contextvar
        cleanup must still happen — otherwise an error path would leak
        the id into all subsequent logs."""
        setup_logging("production")
        log = get_logger("test.exception")
        dep_id = uuid4()

        class _BoomError(Exception):
            pass

        try:
            with bind_deployment(dep_id):
                log.info("during")
                raise _BoomError
        except _BoomError:
            pass
        log.info("after")

        captured = capsys.readouterr()
        before, after = captured.out.strip().split("\n")
        assert dep_id.hex in before
        assert dep_id.hex not in after
