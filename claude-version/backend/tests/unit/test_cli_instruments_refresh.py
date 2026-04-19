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


@pytest.mark.parametrize(
    "symbol",
    [
        "AAPL.NASDAQ",  # equity dotted alias
        "EUR/USD.IDEALPRO",  # FX dotted alias
        "ES.CME",  # bare-root futures dotted alias
        "ESM6.CME",  # month-qualified futures alias (CLI's own ES output)
        "ES.XCME",  # legacy MIC — still accepted for backwards compat
    ],
)
def test_ib_provider_accepts_dotted_and_futures_aliases(
    symbol: str,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """PRD US-006: operators must be able to feed the CLI's own
    ``resolved`` output back in as a re-run. That output contains
    dotted aliases for equity/FX AND month-qualified futures aliases
    (``ESM6.CME``). Preflight must accept all four shapes.
    """
    import msai.cli as cli_mod
    from msai.core.config import Settings

    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setattr(cli_mod, "settings", Settings())

    result = runner.invoke(
        app,
        [
            "instruments",
            "refresh",
            "--symbols",
            symbol,
            "--provider",
            "interactive_brokers",
        ],
    )
    # Preflight must NOT reject (unknown-symbol branch).
    combined = (result.stderr or "") + (result.stdout or "") + result.output
    assert "not in the closed universe" not in combined, combined


@pytest.mark.parametrize(
    "symbol",
    [
        "SPY.NASDAQ",  # known root, wrong venue — exact match fails
        "AAPLXX.NASDAQ",  # unknown root with known venue
        "ESM6",  # month-qualified futures without venue — ambiguous input
        "ES.NASDAQ",  # futures with wrong venue
    ],
)
def test_ib_provider_rejects_malformed_aliases(
    symbol: str,
    runner: CliRunner,
) -> None:
    """Preflight uses exact-match on the accepted-alias set, not
    permissive suffix stripping — inputs that would previously slip
    through (e.g. ``SPY.NASDAQ`` masquerading as bare ``SPY``, or
    ``AAPLXX.NASDAQ`` getting silently normalized to ``AAPL``) must
    be rejected.
    """
    result = runner.invoke(
        app,
        [
            "instruments",
            "refresh",
            "--symbols",
            symbol,
            "--provider",
            "interactive_brokers",
        ],
    )
    assert result.exit_code != 0
    combined = (result.stderr or "") + (result.stdout or "") + result.output
    assert "not in the closed universe" in combined, combined


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


def test_ib_provider_rejects_port_account_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """Preflight validator fires BEFORE any IB connection attempt when
    IB_PORT and IB_ACCOUNT_ID disagree on paper vs live.

    Patches ``msai.cli.settings`` directly — NOT
    ``msai.core.config.settings`` — because ``cli.py`` imports
    ``settings`` at module load, binding the local reference eagerly.
    """
    import msai.cli as cli_mod
    from msai.core.config import Settings

    # Live port + paper account → gotcha #6 silent misroute trap.
    monkeypatch.setenv("IB_PORT", "4001")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setattr(cli_mod, "settings", Settings())

    result = runner.invoke(
        app,
        [
            "instruments",
            "refresh",
            "--symbols",
            "AAPL",
            "--provider",
            "interactive_brokers",
        ],
    )
    assert result.exit_code != 0
    combined = (result.stderr or "") + (result.stdout or "") + result.output
    assert "4001" in combined
    assert "DU1234567" in combined


@pytest.fixture
def _ib_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env setup for IB branch tests. Patches `msai.cli.settings`
    directly — see monkeypatch pattern note on
    test_ib_provider_rejects_port_account_mismatch."""
    import msai.cli as cli_mod
    from msai.core.config import Settings

    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setenv("IB_HOST", "127.0.0.1")
    monkeypatch.setenv("IB_CONNECT_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("IB_REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("IB_INSTRUMENT_CLIENT_ID", "999")
    monkeypatch.setattr(cli_mod, "settings", Settings())


def test_ib_provider_per_symbol_commit_preserves_earlier_successes(
    _ib_env: None,
    runner: CliRunner,
) -> None:
    """PRD US-001 edge case: 'Mid-batch qualification failure (symbol
    #2/5) → Exit non-zero; rows for symbols already qualified are
    committed (idempotent re-run recovers).'

    CLI loops symbol-by-symbol and commits each. A failure on symbol
    2 rolls back only the failed symbol's session state; symbol 1's
    commit is durable. Verified here by counting session.commit() calls.
    """
    mock_client = MagicMock()
    mock_client._is_client_ready = MagicMock()
    mock_client._is_client_ready.wait = AsyncMock(return_value=None)
    mock_client._stop_async = AsyncMock(return_value=None)

    # resolve_for_live succeeds on AAPL, fails on MSFT.
    call_count = {"n": 0}

    async def _fake_resolve(self, symbols: list[str]) -> list[str]:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated IB failure on symbol #2")
        return [f"{s}.NASDAQ" for s in symbols]

    fake_session = MagicMock()
    fake_session.commit = AsyncMock()
    fake_session.rollback = AsyncMock()
    fake_session_cm = MagicMock()
    fake_session_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_cm.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "nautilus_trader.adapters.interactive_brokers.factories.get_cached_ib_client",
            return_value=mock_client,
        ),
        patch(
            "nautilus_trader.adapters.interactive_brokers.factories."
            "get_cached_interactive_brokers_instrument_provider",
            return_value=MagicMock(),
        ),
        patch(
            "msai.services.nautilus.security_master.service.SecurityMaster.resolve_for_live",
            _fake_resolve,
        ),
        patch("msai.cli.async_session_factory", return_value=fake_session_cm),
    ):
        result = runner.invoke(
            app,
            [
                "instruments",
                "refresh",
                "--symbols",
                "AAPL,MSFT",
                "--provider",
                "interactive_brokers",
            ],
        )

    # Exit non-zero (symbol 2 failed).
    assert result.exit_code != 0
    # But: symbol 1's success was committed (1 commit), symbol 2's
    # rollback fired once (for the failed call).
    assert fake_session.commit.await_count == 1
    assert fake_session.rollback.await_count == 1


def test_ib_provider_happy_path_calls_factory_and_resolve(
    _ib_env: None,
    runner: CliRunner,
) -> None:
    """AAPL qualifies via the mocked factory chain + IBQualifier, then
    SecurityMaster.resolve_for_live commits. Exit 0. Asserts:
    - CORRECT factory kwargs (host, port, client_id, request_timeout_secs)
    - client.start() is NOT called (factory already starts the client)
    - client.stop() is NOT called (we await _stop_async directly)
    - _stop_async IS awaited in finally (even on failure).
    """
    # Mock the factory chain
    mock_client = MagicMock()
    mock_client._is_client_ready = MagicMock()
    mock_client._is_client_ready.wait = AsyncMock(return_value=None)
    mock_client.start = MagicMock()
    mock_client.stop = MagicMock()
    mock_client._stop_async = AsyncMock(return_value=None)

    mock_provider = MagicMock()

    mock_get_client = MagicMock(return_value=mock_client)
    mock_get_provider = MagicMock(return_value=mock_provider)

    # Mock SecurityMaster.resolve_for_live so the test doesn't hit a DB.
    async def _fake_resolve(self, symbols: list[str]) -> list[str]:
        return [f"{s}.NASDAQ" for s in symbols]

    fake_session = MagicMock()
    fake_session.commit = AsyncMock()
    fake_session.rollback = AsyncMock()
    fake_session_cm = MagicMock()
    fake_session_cm.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session_cm.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "nautilus_trader.adapters.interactive_brokers.factories.get_cached_ib_client",
            mock_get_client,
        ),
        patch(
            "nautilus_trader.adapters.interactive_brokers.factories."
            "get_cached_interactive_brokers_instrument_provider",
            mock_get_provider,
        ),
        patch(
            "msai.services.nautilus.security_master.service.SecurityMaster.resolve_for_live",
            _fake_resolve,
        ),
        patch("msai.cli.async_session_factory", return_value=fake_session_cm),
    ):
        result = runner.invoke(
            app,
            [
                "instruments",
                "refresh",
                "--symbols",
                "AAPL",
                "--provider",
                "interactive_brokers",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "AAPL" in result.output

    # --- Factory kwargs correctness ---
    assert mock_get_client.called
    kwargs = mock_get_client.call_args.kwargs
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 4002
    assert kwargs["client_id"] == 999
    assert kwargs["request_timeout_secs"] == 30

    # --- Lifecycle correctness (Codex plan-review iter 1 P1s) ---
    # Factory already calls client.start() internally; CLI must NOT call it.
    mock_client.start.assert_not_called()
    # Public stop() only schedules the async stop; CLI must NOT call it.
    mock_client.stop.assert_not_called()
    # _stop_async MUST have been awaited directly.
    mock_client._stop_async.assert_awaited_once()


def test_ib_provider_dead_gateway_times_out_with_operator_hint(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """When the IB client never reaches ready state, CLI times out in
    the short connect-timeout window, prints an operator hint naming
    all relevant env vars, and still awaits _stop_async in teardown."""
    import asyncio

    import msai.cli as cli_mod
    from msai.core.config import Settings

    # 1s connect timeout for fast test; paper env so preflight passes.
    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setenv("IB_HOST", "127.0.0.1")
    monkeypatch.setenv("IB_CONNECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("IB_REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("IB_INSTRUMENT_CLIENT_ID", "999")
    monkeypatch.setattr(cli_mod, "settings", Settings())

    mock_client = MagicMock()

    async def _never() -> None:
        await asyncio.sleep(3600)

    mock_client._is_client_ready = MagicMock()
    mock_client._is_client_ready.wait = _never
    mock_client.start = MagicMock()
    mock_client.stop = MagicMock()
    mock_client._stop_async = AsyncMock(return_value=None)

    with (
        patch(
            "nautilus_trader.adapters.interactive_brokers.factories.get_cached_ib_client",
            return_value=mock_client,
        ),
        patch(
            "nautilus_trader.adapters.interactive_brokers.factories."
            "get_cached_interactive_brokers_instrument_provider",
            return_value=MagicMock(),
        ),
    ):
        result = runner.invoke(
            app,
            [
                "instruments",
                "refresh",
                "--symbols",
                "AAPL",
                "--provider",
                "interactive_brokers",
            ],
        )

    assert result.exit_code != 0
    combined = (result.stderr or "") + (result.stdout or "") + result.output
    # Operator hint names all 4 env vars (either by name or by value).
    assert "IB_HOST" in combined or "127.0.0.1" in combined
    assert "IB_PORT" in combined or "4002" in combined
    assert "IB_ACCOUNT_ID" in combined or "DU1234567" in combined
    assert "IB_INSTRUMENT_CLIENT_ID" in combined or "999" in combined
    # Teardown still ran — _stop_async awaited even on timeout.
    mock_client._stop_async.assert_awaited_once()
    # stop() must NOT have been called (avoids double _stop_async).
    mock_client.stop.assert_not_called()
