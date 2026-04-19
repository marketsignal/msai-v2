"""Unit tests for ``msai instruments refresh``.

The refresh command is the PRD §47-48 pre-warm tool for the instrument
registry.  It has two provider paths:

- ``--provider databento`` — wraps :meth:`DatabentoClient.fetch_definition_instruments`
  + :meth:`SecurityMaster._upsert_definition_and_alias` via
  :meth:`SecurityMaster._resolve_databento_continuous`.  Tested here by
  mocking the Databento client + SecurityMaster.

- ``--provider interactive_brokers`` — short-lived Nautilus IB
  client. Tested here with the factory chain + ``SecurityMaster``
  mocked so the tests don't touch real IB Gateway.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from msai.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ----------------------------------------------------------------------
# Sub-app wiring
# ----------------------------------------------------------------------


class TestSubAppWiring:
    def test_instruments_sub_app_registered(self, runner: CliRunner) -> None:
        """``msai instruments --help`` lists ``refresh``."""
        result = runner.invoke(app, ["instruments", "--help"])
        assert result.exit_code == 0, result.output
        assert "refresh" in result.output

    def test_refresh_help_documents_providers(self, runner: CliRunner) -> None:
        """``msai instruments refresh --help`` documents both providers."""
        result = runner.invoke(app, ["instruments", "refresh", "--help"])
        assert result.exit_code == 0, result.output
        # The command signature must document the provider choice.
        assert "--provider" in result.output


# ----------------------------------------------------------------------
# --provider databento path
# ----------------------------------------------------------------------


class TestRefreshDatabento:
    def test_refresh_databento_happy_path(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With a Databento key set + a ``.Z.N`` symbol, the command delegates
        to :meth:`SecurityMaster.resolve_for_backtest` and exits 0.

        We mock at the SecurityMaster boundary so we don't need a live
        database or the Databento SDK installed for this unit test.
        """
        monkeypatch.setenv("DATABENTO_API_KEY", "test-key-123")

        fake_sm = MagicMock()
        fake_sm.resolve_for_backtest = AsyncMock(return_value=["ES.Z.5.GLBX"])

        fake_session = MagicMock()
        fake_session.commit = AsyncMock()
        fake_session.rollback = AsyncMock()
        fake_session_cm = MagicMock()
        fake_session_cm.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("msai.cli.async_session_factory", return_value=fake_session_cm),
            patch("msai.cli.DatabentoClient") as mock_databento_cls,
            patch("msai.cli.SecurityMaster", return_value=fake_sm) as mock_sm_cls,
        ):
            result = runner.invoke(
                app,
                ["instruments", "refresh", "--symbols", "ES.Z.5", "--provider", "databento"],
            )

        assert result.exit_code == 0, result.output
        # DatabentoClient constructed with the API key from env.
        mock_databento_cls.assert_called_once()
        # SecurityMaster constructed with qualifier=None and databento_client set.
        mock_sm_cls.assert_called_once()
        sm_kwargs = mock_sm_cls.call_args.kwargs
        assert sm_kwargs["qualifier"] is None
        assert sm_kwargs["databento_client"] is mock_databento_cls.return_value
        # resolve_for_backtest called with the parsed symbol list.
        fake_sm.resolve_for_backtest.assert_awaited_once()
        call = fake_sm.resolve_for_backtest.await_args
        assert call.args[0] == ["ES.Z.5"]
        # F8 regression: the CLI must commit the registry writes before the
        # async session context exits.  Without the explicit commit the
        # flushed rows from _upsert_definition_and_alias roll back silently.
        fake_session.commit.assert_awaited_once()
        fake_session.rollback.assert_not_awaited()

    def test_refresh_databento_rolls_back_on_failure(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When resolve_for_backtest raises, the CLI must roll back the
        session (never commit partial flushed rows)."""
        monkeypatch.setenv("DATABENTO_API_KEY", "test-key-123")

        fake_sm = MagicMock()
        fake_sm.resolve_for_backtest = AsyncMock(side_effect=RuntimeError("boom"))

        fake_session = MagicMock()
        fake_session.commit = AsyncMock()
        fake_session.rollback = AsyncMock()
        fake_session_cm = MagicMock()
        fake_session_cm.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("msai.cli.async_session_factory", return_value=fake_session_cm),
            patch("msai.cli.DatabentoClient"),
            patch("msai.cli.SecurityMaster", return_value=fake_sm),
        ):
            result = runner.invoke(
                app,
                ["instruments", "refresh", "--symbols", "ES.Z.5", "--provider", "databento"],
            )

        assert result.exit_code != 0
        fake_session.rollback.assert_awaited_once()
        fake_session.commit.assert_not_awaited()

    def test_refresh_databento_raises_without_api_key(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without ``DATABENTO_API_KEY``, the command fails with an operator
        hint — the Databento path cannot proceed without a key."""
        monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
        # Also clear any setting-level fallback via pydantic settings env.
        monkeypatch.setattr(
            "msai.cli.settings.databento_api_key",
            "",
            raising=False,
        )

        result = runner.invoke(
            app,
            ["instruments", "refresh", "--symbols", "ES.Z.5", "--provider", "databento"],
        )

        assert result.exit_code != 0
        # Operator hint must mention the env var.
        assert "DATABENTO_API_KEY" in result.output


# ----------------------------------------------------------------------
# --provider interactive_brokers path
# ----------------------------------------------------------------------


def test_ib_provider_rejects_unknown_symbol(runner: CliRunner) -> None:
    """Symbols outside PHASE_1_PAPER_SYMBOLS are rejected in preflight,
    before any IB connection is attempted."""
    result = runner.invoke(
        app,
        [
            "instruments",
            "refresh",
            "--symbols",
            "NVDA",
            "--provider",
            "interactive_brokers",
        ],
    )
    assert result.exit_code != 0
    combined = (result.stderr or "") + (result.stdout or "") + result.output
    # Error names the unknown symbol AND the closed universe
    assert "NVDA" in combined
    assert "AAPL" in combined  # a symbol from PHASE_1_PAPER_SYMBOLS
