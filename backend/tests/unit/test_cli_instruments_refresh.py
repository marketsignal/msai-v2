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

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from msai.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ----------------------------------------------------------------------
# _build_ib_contract_for_symbol — per-asset-class factories
# ----------------------------------------------------------------------


class TestBuildIBContractForSymbol:
    """Per-asset-class IBContract factories replace the closed-universe
    canonical_instrument_id() map. STK / FUT / CASH are the v1 scope;
    FUT is restricted to the closed CME E-mini quarterly set
    {ES, NQ, RTY, YM} because ``current_quarterly_expiry`` is only
    correct for that cycle (CL/GC/ZB use different cycles and venues
    and need operator overrides v1 doesn't surface)."""

    def test_build_ib_contract_for_stk(self) -> None:
        """STK factory builds a SMART-routed equity contract."""
        from msai.cli import _build_ib_contract_for_symbol

        contract = _build_ib_contract_for_symbol("AAPL", asset_class="stk", today=date(2026, 4, 27))
        assert contract.secType == "STK"
        assert contract.symbol == "AAPL"
        assert contract.exchange == "SMART"
        assert contract.primaryExchange == "NASDAQ"
        assert contract.currency == "USD"

    def test_build_ib_contract_for_stk_with_arca_override(self) -> None:
        """ETFs like SPY/VTI need ``--primary-exchange ARCA``."""
        from msai.cli import _build_ib_contract_for_symbol

        contract = _build_ib_contract_for_symbol(
            "SPY",
            asset_class="stk",
            today=date(2026, 4, 27),
            primary_exchange="ARCA",
        )
        assert contract.secType == "STK"
        assert contract.symbol == "SPY"
        assert contract.exchange == "SMART"
        assert contract.primaryExchange == "ARCA"
        assert contract.currency == "USD"

    def test_build_ib_contract_for_fut(self) -> None:
        """FUT factory builds a CME futures contract with quarterly expiry.
        On 2026-04-27, the next quarterly expiry is 2026-06 (third
        Friday is 2026-06-19)."""
        from msai.cli import _build_ib_contract_for_symbol

        contract = _build_ib_contract_for_symbol("ES", asset_class="fut", today=date(2026, 4, 27))
        assert contract.secType == "FUT"
        assert contract.symbol == "ES"
        assert contract.exchange == "CME"
        # YYYYMM lets IB resolve the holiday-adjusted last-trade date.
        assert contract.lastTradeDateOrContractMonth == "202606"
        assert contract.currency == "USD"

    def test_build_ib_contract_for_cash(self) -> None:
        """CASH factory builds an IDEALPRO forex contract; BASE/QUOTE
        splits into symbol=base, currency=quote."""
        from msai.cli import _build_ib_contract_for_symbol

        contract = _build_ib_contract_for_symbol(
            "EUR/USD", asset_class="cash", today=date(2026, 4, 27)
        )
        assert contract.secType == "CASH"
        assert contract.symbol == "EUR"
        assert contract.exchange == "IDEALPRO"
        assert contract.currency == "USD"

    def test_build_ib_contract_for_cash_no_slash_defaults_quote_usd(self) -> None:
        """A bare base symbol (no slash) defaults the quote to USD."""
        from msai.cli import _build_ib_contract_for_symbol

        contract = _build_ib_contract_for_symbol("EUR", asset_class="cash", today=date(2026, 4, 27))
        assert contract.symbol == "EUR"
        assert contract.currency == "USD"

    def test_build_ib_contract_unknown_asset_class_raises(self) -> None:
        """Unknown asset_class raises ValueError naming the supported set."""
        from msai.cli import _build_ib_contract_for_symbol

        with pytest.raises(ValueError, match="Unknown asset class"):
            _build_ib_contract_for_symbol("XYZ", asset_class="bogus", today=date(2026, 4, 27))

    def test_build_ib_contract_unsupported_fut_root_raises(self) -> None:
        """v1 rejects non-CME-quarterly futures roots (CL, GC, ZB, etc.)
        because ``current_quarterly_expiry`` is only correct for the
        ES/NQ/RTY/YM cycle."""
        from msai.cli import _build_ib_contract_for_symbol

        with pytest.raises(ValueError, match=r"v1 supports.*ES.*NQ.*RTY.*YM"):
            _build_ib_contract_for_symbol("CL", asset_class="fut", today=date(2026, 4, 27))

    def test_build_ib_contract_for_all_supported_fut_roots(self) -> None:
        """ES, NQ, RTY, YM all build successfully — the v1 closed
        quarterly CME E-mini set."""
        from msai.cli import _build_ib_contract_for_symbol

        for root in ("ES", "NQ", "RTY", "YM"):
            contract = _build_ib_contract_for_symbol(
                root, asset_class="fut", today=date(2026, 4, 27)
            )
            assert contract.secType == "FUT"
            assert contract.symbol == root
            assert contract.exchange == "CME"


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
        # The command signature must document the provider choice. Strip ANSI
        # color codes because Rich wraps option names in color sequences that
        # would split "--provider" across codes (e.g. "\x1b[1;36m-\x1b[0m\x1b[1;36m-provider\x1b[0m")
        # and break a naive substring match. Observed in CI where isatty
        # detection differs from local dev shells.
        import re

        stripped = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "--provider" in stripped


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


def test_cli_instruments_refresh_accepts_asset_class_flag(runner: CliRunner) -> None:
    """The CLI exposes ``--asset-class`` and surfaces it in --help."""
    result = runner.invoke(app, ["instruments", "refresh", "--help"])
    assert result.exit_code == 0, result.output
    # Strip ANSI color codes — Rich wraps option names in color sequences.
    import re

    stripped = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert "--asset-class" in stripped


def test_cli_instruments_refresh_builds_contracts_for_supported_fut_roots(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """``--asset-class fut --symbols ES,NQ`` builds two CME FUT contracts
    and threads them through ``_run_ib_resolve_for_live`` as
    ``list[IBContract]`` (not ``list[str]``).

    v1 scopes FUT to the closed quarterly CME E-mini set (ES, NQ, RTY, YM)
    because ``current_quarterly_expiry`` is only correct for that cycle
    (live_instrument_bootstrap.py:87-93). Other futures roots
    (e.g. CL/NYMEX, GC/COMEX, ZB/CBOT) require operator overrides v1
    does not surface — ``_build_ib_contract_for_symbol`` rejects them.
    """
    import msai.cli as cli_mod
    from msai.core.config import Settings

    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setattr(cli_mod, "settings", Settings())

    captured_contracts: list = []

    async def fake_run_ib_resolve_for_live(contracts):  # type: ignore[no-untyped-def]
        captured_contracts.extend(contracts)
        return ["ESM6.CME", "NQM6.CME"]

    monkeypatch.setattr(cli_mod, "_run_ib_resolve_for_live", fake_run_ib_resolve_for_live)

    result = runner.invoke(
        app,
        [
            "instruments",
            "refresh",
            "--provider",
            "interactive_brokers",
            "--symbols",
            "ES,NQ",
            "--asset-class",
            "fut",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(captured_contracts) == 2
    assert all(c.secType == "FUT" and c.exchange == "CME" for c in captured_contracts)
    assert {c.symbol for c in captured_contracts} == {"ES", "NQ"}


def test_cli_instruments_refresh_rejects_unsupported_fut_root(runner: CliRunner) -> None:
    """v1 rejects non-CME-quarterly futures roots (CL, GC, ZB, etc.)
    at the factory boundary — error message names the supported set."""
    from msai.cli import _build_ib_contract_for_symbol

    with pytest.raises(ValueError, match=r"v1 supports.*ES.*NQ.*RTY.*YM"):
        _build_ib_contract_for_symbol("CL", asset_class="fut", today=date(2026, 4, 27))


def test_cli_instruments_refresh_default_asset_class_is_stk(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """No ``--asset-class`` flag → defaults to STK; ``--symbols AAPL,MSFT``
    builds two SMART-routed equity contracts with primaryExchange=NASDAQ."""
    import msai.cli as cli_mod
    from msai.core.config import Settings

    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setattr(cli_mod, "settings", Settings())

    captured_contracts: list = []

    async def fake_run_ib_resolve_for_live(contracts):  # type: ignore[no-untyped-def]
        captured_contracts.extend(contracts)
        return ["AAPL.NASDAQ", "MSFT.NASDAQ"]

    monkeypatch.setattr(cli_mod, "_run_ib_resolve_for_live", fake_run_ib_resolve_for_live)

    result = runner.invoke(
        app,
        [
            "instruments",
            "refresh",
            "--provider",
            "interactive_brokers",
            "--symbols",
            "AAPL,MSFT",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(captured_contracts) == 2
    assert all(
        c.secType == "STK"
        and c.exchange == "SMART"
        and c.primaryExchange == "NASDAQ"
        and c.currency == "USD"
        for c in captured_contracts
    )


def test_cli_instruments_refresh_primary_exchange_override(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """``--primary-exchange ARCA`` flows through to the STK contract;
    needed for ETFs like SPY/VTI listed on ARCA."""
    import msai.cli as cli_mod
    from msai.core.config import Settings

    monkeypatch.setenv("IB_PORT", "4002")
    monkeypatch.setenv("IB_ACCOUNT_ID", "DU1234567")
    monkeypatch.setattr(cli_mod, "settings", Settings())

    captured_contracts: list = []

    async def fake_run_ib_resolve_for_live(contracts):  # type: ignore[no-untyped-def]
        captured_contracts.extend(contracts)
        return ["SPY.ARCA"]

    monkeypatch.setattr(cli_mod, "_run_ib_resolve_for_live", fake_run_ib_resolve_for_live)

    result = runner.invoke(
        app,
        [
            "instruments",
            "refresh",
            "--provider",
            "interactive_brokers",
            "--symbols",
            "SPY",
            "--primary-exchange",
            "ARCA",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(captured_contracts) == 1
    assert captured_contracts[0].symbol == "SPY"
    assert captured_contracts[0].primaryExchange == "ARCA"


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


def test_ib_provider_failure_in_run_ib_resolve_for_live_exits_nonzero(
    _ib_env: None,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD US-001 edge case: 'Mid-batch qualification failure → Exit
    non-zero.'

    Stubs ``_run_ib_resolve_for_live`` at the function boundary to raise.
    Per-symbol commit/rollback discipline is enforced INSIDE
    ``_run_ib_resolve_for_live`` (it owns the session lifecycle now);
    that body's commit/rollback semantics are exercised by the gated
    paper-IB smoke test ``test_instruments_refresh_ib_smoke.py``.
    """
    import msai.cli as cli_mod

    async def fake_run_ib_resolve_for_live(contracts):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated IB failure")

    monkeypatch.setattr(cli_mod, "_run_ib_resolve_for_live", fake_run_ib_resolve_for_live)

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

    assert result.exit_code != 0


def test_ib_provider_happy_path_emits_resolved_canonicals(
    _ib_env: None,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: AAPL flows through the CLI's IB branch and the
    resolved canonicals are emitted. ``_run_ib_resolve_for_live`` is
    stubbed at the function boundary; factory + lifecycle assertions
    are covered by ``test_ib_provider_dead_gateway_times_out_with_operator_hint``
    which exercises the real factory."""
    import msai.cli as cli_mod

    async def fake_run_ib_resolve_for_live(contracts):  # type: ignore[no-untyped-def]
        return [f"{c.symbol}.NASDAQ" for c in contracts]

    monkeypatch.setattr(cli_mod, "_run_ib_resolve_for_live", fake_run_ib_resolve_for_live)

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
    assert "AAPL.NASDAQ" in result.output


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
