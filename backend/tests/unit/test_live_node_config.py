"""Unit tests for the live TradingNodeConfig builder (Phase 1 task 1.5).

Verifies the builder produces a config that:

- Uses Nautilus native engine configs (Live*EngineConfig) with the
  reconciliation + risk settings from the plan
- Wires the IB data + exec clients with the v9 instrument bootstrap
- Gives each deployment unique ``ibg_client_id`` values for both data
  and exec clients (gotcha #3 — IB Gateway silently disconnects on
  collision)
- Validates port/account-id consistency (gotcha #6 — paper port + live
  account is a silent data-flow killer)
- Pins ``trader_id`` to a deployment-specific value
"""

from __future__ import annotations

import pytest

from msai.services.nautilus.live_node_config import (
    IBSettings,
    build_live_trading_node_config,
)

# 16-char hex slugs matching the shape Task 1.1b persists on
# LiveDeployment.deployment_slug (generate_deployment_slug returns
# ``secrets.token_hex(8)``). Fixed for deterministic tests.
_SLUG_A = "a1b2c3d4e5f60718"
_SLUG_B = "bbbbccccddddeeee"

# Real Nautilus StrategyConfig from the example strategy bundled with
# the project. Using the real class (not a placeholder) means the tests
# exercise the full ImportableStrategyConfig contract — Nautilus's
# resolve_config_path() requires a NautilusConfig subclass.
_EXAMPLE_STRATEGY_PATH = "strategies.example.ema_cross:EMACrossStrategy"
_EXAMPLE_CONFIG_PATH = "strategies.example.config:EMACrossConfig"


def _paper_settings(account: str = "DU1234567") -> IBSettings:
    return IBSettings(host="127.0.0.1", port=4002, account_id=account)


def _live_settings(account: str = "U1234567") -> IBSettings:
    return IBSettings(host="127.0.0.1", port=4001, account_id=account)


@pytest.fixture
def _strategies_on_path() -> None:
    """Put the project's ``strategies/`` directory on ``sys.path`` so
    ``import strategies.example.config`` works during the round-trip
    tests below. The strategy_registry does this at runtime when it
    discovers strategies; tests that exercise the import path need to
    do it explicitly because they bypass the registry."""
    import sys
    from pathlib import Path

    # backend/tests/unit/test_live_node_config.py → up four to the
    # claude-version root, where strategies/ lives.
    strategies_parent = str(Path(__file__).resolve().parents[3])
    if strategies_parent not in sys.path:
        sys.path.insert(0, strategies_parent)


def _build(
    *,
    deployment_slug: str = _SLUG_A,
    strategy_config: dict | None = None,
    paper_symbols: list[str] | None = None,
    ib_settings: IBSettings | None = None,
):
    """Build a TradingNodeConfig with sensible test defaults so every
    test only has to pass the field(s) it actually exercises."""
    return build_live_trading_node_config(
        deployment_slug=deployment_slug,
        strategy_path=_EXAMPLE_STRATEGY_PATH,
        strategy_config_path=_EXAMPLE_CONFIG_PATH,
        strategy_config=strategy_config if strategy_config is not None else {},
        paper_symbols=paper_symbols if paper_symbols is not None else ["AAPL"],
        ib_settings=ib_settings if ib_settings is not None else _paper_settings(),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_returns_trading_node_config(self) -> None:
        from nautilus_trader.live.config import TradingNodeConfig

        config = _build(strategy_config={"fast_ema_period": 10, "slow_ema_period": 30})
        assert isinstance(config, TradingNodeConfig)

    def test_trader_id_matches_stable_identity_slug(self) -> None:
        """Codex Task 1.5 iter2 P2 regression: ``trader_id`` must be
        ``MSAI-{deployment_slug}`` so it matches the value Task 1.1b
        persists on ``LiveDeployment.trader_id``. A mismatch silently
        breaks warm-restart state reload and the projection consumer's
        stream lookup."""
        config = _build()
        assert config.trader_id.value == f"MSAI-{_SLUG_A}"

    def test_strategies_list_contains_importable_strategy_config(self) -> None:
        from nautilus_trader.config import ImportableStrategyConfig

        config = _build()
        assert len(config.strategies) == 1
        strat = config.strategies[0]
        assert isinstance(strat, ImportableStrategyConfig)
        assert strat.strategy_path == _EXAMPLE_STRATEGY_PATH
        # The config_path now points at the real EMACrossConfig so
        # Nautilus's resolve_config_path() accepts it. The old stub was
        # a Pydantic BaseModel which failed the subclass check at
        # runtime (Codex Task 1.5 review P1 fix).
        assert strat.config_path == _EXAMPLE_CONFIG_PATH

    def test_manage_stop_injected_as_true(self) -> None:
        """Task 1.10 + gotcha #13: the builder MUST inject
        ``manage_stop=True`` on top of the caller's config so Nautilus
        flattens positions via its native market-exit loop on stop
        (no custom on_stop)."""
        config = _build(strategy_config={"fast_ema_period": 10})
        strat = config.strategies[0]
        assert strat.config["manage_stop"] is True

    def test_order_id_tag_injected_from_slug(self) -> None:
        """Task 1.10: ``order_id_tag`` must be ``0-{deployment_slug}``
        so Nautilus emits ``{class}-0-{slug}`` which matches
        ``derive_strategy_id_full(class, slug, 0)``."""
        config = _build()
        strat = config.strategies[0]
        assert strat.config["order_id_tag"] == f"0-{_SLUG_A}"

    def test_caller_config_keys_preserved(self) -> None:
        """The injected ``manage_stop`` and ``order_id_tag`` MUST be
        added on top of the caller's config, not replace it."""
        config = _build(
            strategy_config={
                "fast_ema_period": 42,
                "slow_ema_period": 100,
            }
        )
        strat = config.strategies[0]
        assert strat.config["fast_ema_period"] == 42
        assert strat.config["slow_ema_period"] == 100
        assert strat.config["manage_stop"] is True
        assert strat.config["order_id_tag"] == f"0-{_SLUG_A}"

    def test_caller_cannot_override_manage_stop_false(self) -> None:
        """Defensive: the caller's config is spread BEFORE the
        injected fields, so ``manage_stop=True`` always wins. A
        misconfigured caller that passes ``manage_stop=False`` must
        not be able to disable the native flatten-on-stop behavior
        (that's a safety invariant for live trading)."""
        config = _build(strategy_config={"manage_stop": False})
        strat = config.strategies[0]
        assert strat.config["manage_stop"] is True

    def test_engines_use_nautilus_native_configs(self) -> None:
        from nautilus_trader.live.config import (
            LiveDataEngineConfig,
            LiveExecEngineConfig,
            LiveRiskEngineConfig,
        )

        config = _build()
        assert isinstance(config.data_engine, LiveDataEngineConfig)
        assert isinstance(config.exec_engine, LiveExecEngineConfig)
        assert isinstance(config.risk_engine, LiveRiskEngineConfig)

    def test_exec_engine_reconciliation_enabled(self) -> None:
        """Reconciliation MUST be on for live (gotcha #19 — IB may have
        fills we don't know about after a crash). Lookback default of
        1 day (1440 mins) is the plan spec."""
        config = _build()
        assert config.exec_engine.reconciliation is True
        assert config.exec_engine.reconciliation_lookback_mins == 1440

    def test_risk_engine_not_bypassed(self) -> None:
        """``bypass=False`` is the safe default — the risk engine MUST
        run on every order. Disabling it is a configuration accident
        we never want to ship."""
        config = _build()
        assert config.risk_engine.bypass is False

    def test_phase_3_wires_cache_and_message_bus(self) -> None:
        """Phase 3 tasks 3.1 + 3.2 wire ``CacheConfig`` and
        ``MessageBusConfig`` to Redis so live state can be
        rehydrated by the projection layer after a FastAPI
        restart. Detailed assertions live in
        ``test_live_node_config_cache.py``; this is a smoke
        check that BOTH configs are now non-None."""
        config = _build()
        assert config.cache is not None
        assert config.message_bus is not None

    def test_phase_4_state_persistence_enabled(self) -> None:
        """Phase 4 task 4.1 enables ``load_state`` / ``save_state``
        so a restarted subprocess picks up exactly where the
        previous one left off. Both default to False on
        ``TradingNodeConfig`` despite the docstring claiming True
        (Codex gotcha #10) — the builder MUST set them
        explicitly."""
        config = _build()
        assert config.load_state is True
        assert config.save_state is True

    def test_config_path_resolves_to_real_nautilus_config(self, _strategies_on_path: None) -> None:
        """Codex Task 1.5 review P1 regression guard: Nautilus's
        ``resolve_config_path()`` accepts the config_path the builder
        wires. This is the same lookup ``StrategyFactory.create()`` does
        on the subprocess side, so a pass here means a live deployment
        wouldn't crash immediately on strategy instantiation.
        """
        from nautilus_trader.common.config import resolve_config_path
        from nautilus_trader.config import NautilusConfig

        config = _build()
        strat = config.strategies[0]
        resolved = resolve_config_path(strat.config_path)
        assert issubclass(resolved, NautilusConfig), (
            f"config_path {strat.config_path!r} must resolve to a NautilusConfig "
            f"subclass; got {resolved!r}"
        )

    def test_strategy_config_round_trips_through_real_config_class(
        self, _strategies_on_path: None
    ) -> None:
        """End-to-end P1 regression guard: encode the ``config`` dict
        through ``msgspec.json`` and ``NautilusConfig.parse()`` exactly
        the way ``StrategyFactory.create()`` does, then assert the
        typed fields come back out. Proves the builder's config_path
        actually works at runtime, not just structurally."""
        import msgspec
        from nautilus_trader.common.config import msgspec_encoding_hook, resolve_config_path

        config = _build(
            strategy_config={
                "instrument_id": "AAPL.NASDAQ",
                "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-INTERNAL",
                "fast_ema_period": 12,
                "slow_ema_period": 26,
                "trade_size": "1",
            },
        )
        strat = config.strategies[0]
        config_cls = resolve_config_path(strat.config_path)
        encoded = msgspec.json.encode(strat.config, enc_hook=msgspec_encoding_hook)
        parsed = config_cls.parse(encoded)
        assert parsed.fast_ema_period == 12
        assert parsed.slow_ema_period == 26


# ---------------------------------------------------------------------------
# IB data + exec client wiring
# ---------------------------------------------------------------------------


class TestIBClientWiring:
    def test_data_and_exec_clients_present_under_ib_venue(self) -> None:
        from nautilus_trader.adapters.interactive_brokers.config import (
            InteractiveBrokersDataClientConfig,
            InteractiveBrokersExecClientConfig,
        )

        config = _build()
        assert "INTERACTIVE_BROKERS" in config.data_clients
        assert "INTERACTIVE_BROKERS" in config.exec_clients

        data_client = config.data_clients["INTERACTIVE_BROKERS"]
        exec_client = config.exec_clients["INTERACTIVE_BROKERS"]
        assert isinstance(data_client, InteractiveBrokersDataClientConfig)
        assert isinstance(exec_client, InteractiveBrokersExecClientConfig)

    def test_exec_client_carries_account_id(self) -> None:
        config = _build(ib_settings=_paper_settings(account="DU7654321"))
        # Nautilus prefixes the account_id internally with the IB account
        # type, but the configured field is the bare id we passed in.
        assert "DU7654321" in config.exec_clients["INTERACTIVE_BROKERS"].account_id

    def test_data_and_exec_use_distinct_ibg_client_ids(self) -> None:
        """Gotcha #3: two TradingNode clients on the same IB Gateway
        with the same client_id silently disconnect each other. Data
        and exec on the same deployment MUST use distinct ids."""
        config = _build()
        data_id = config.data_clients["INTERACTIVE_BROKERS"].ibg_client_id
        exec_id = config.exec_clients["INTERACTIVE_BROKERS"].ibg_client_id
        assert data_id != exec_id

    def test_two_deployments_have_distinct_data_client_ids(self) -> None:
        """Same gotcha #3 — concurrent deployments on the same IB
        Gateway must not collide on ``ibg_client_id``."""
        config_a = _build(deployment_slug=_SLUG_A)
        config_b = _build(deployment_slug=_SLUG_B)
        assert (
            config_a.data_clients["INTERACTIVE_BROKERS"].ibg_client_id
            != config_b.data_clients["INTERACTIVE_BROKERS"].ibg_client_id
        )
        assert (
            config_a.exec_clients["INTERACTIVE_BROKERS"].ibg_client_id
            != config_b.exec_clients["INTERACTIVE_BROKERS"].ibg_client_id
        )

    def test_ibg_client_id_is_stable_across_calls_for_same_deployment(self) -> None:
        """Restarting the SAME deployment must yield the SAME client_id —
        otherwise IB Gateway sees a "new" client and the old one's
        bookkeeping (open orders, subscriptions) gets stranded.
        Derived deterministically from the slug."""
        a1 = _build(deployment_slug=_SLUG_A)
        a2 = _build(deployment_slug=_SLUG_A)
        assert (
            a1.data_clients["INTERACTIVE_BROKERS"].ibg_client_id
            == a2.data_clients["INTERACTIVE_BROKERS"].ibg_client_id
        )

    def test_ibg_host_and_port_propagate(self) -> None:
        config = _build(
            ib_settings=IBSettings(host="ib-gateway.internal", port=4002, account_id="DU1111111"),
        )
        data_client = config.data_clients["INTERACTIVE_BROKERS"]
        exec_client = config.exec_clients["INTERACTIVE_BROKERS"]
        assert data_client.ibg_host == "ib-gateway.internal"
        assert data_client.ibg_port == 4002
        assert exec_client.ibg_host == "ib-gateway.internal"
        assert exec_client.ibg_port == 4002


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_paper_symbols_raises(self) -> None:
        """A trading node with zero subscribed instruments is a
        configuration accident — fail at build time, not at the first
        bar event that doesn't arrive."""
        with pytest.raises(ValueError, match="paper_symbols"):
            _build(paper_symbols=[])

    def test_paper_port_with_live_account_raises(self) -> None:
        """Gotcha #6: port 4002 (paper) + a live account id is a silent
        data-flow killer — IB Gateway accepts the connection but
        provides no data. Fail loudly at config-build time.

        Integration-level regression — detailed validator coverage
        lives in ``test_ib_port_validator.py``."""
        with pytest.raises(ValueError, match="paper port .* live-prefix"):
            _build(ib_settings=IBSettings(host="127.0.0.1", port=4002, account_id="U1234567"))

    def test_live_port_with_paper_account_raises(self) -> None:
        """Inverse of the previous case: port 4001 (live) + a paper
        account id (``DU...``) is also wrong and equally silent."""
        with pytest.raises(ValueError, match="live port .* paper-prefix"):
            _build(ib_settings=IBSettings(host="127.0.0.1", port=4001, account_id="DU1234567"))

    def test_paper_port_with_paper_account_succeeds(self) -> None:
        assert _build() is not None

    def test_live_port_with_live_account_succeeds(self) -> None:
        assert _build(ib_settings=_live_settings()) is not None

    def test_unknown_port_raises(self) -> None:
        """Only 4001 (live) and 4002 (paper) are recognized — anything
        else is a typo we should catch at build time."""
        with pytest.raises(ValueError, match="unknown IB port"):
            _build(ib_settings=IBSettings(host="127.0.0.1", port=9999, account_id="DU1234567"))

    def test_unknown_paper_symbol_raises(self) -> None:
        """Bubbles up the bootstrap helper's ValueError so callers don't
        have to import two modules just to surface the same error."""
        with pytest.raises(ValueError, match="not registered in PHASE_1_PAPER_SYMBOLS"):
            _build(paper_symbols=["XYZ"])

    def test_blank_account_id_raises(self) -> None:
        """Codex Task 1.5 review P2 fix: a blank or whitespace-only
        account id must be rejected before the gotcha-#6 prefix check
        — otherwise an empty string is silently classified as 'live'
        and a paper-port deployment goes through the live-account
        branch."""
        with pytest.raises(ValueError, match="empty"):
            _build(ib_settings=IBSettings(host="127.0.0.1", port=4002, account_id="   "))

    def test_whitespace_padded_paper_account_classified_correctly(self) -> None:
        """Codex Task 1.5 review P2 fix: ``' DU1234567'`` from a
        misformatted ``.env`` file must classify as paper, not live —
        otherwise the gotcha-#6 guard would falsely reject a valid
        paper deployment as 'live account on paper port'."""
        # This would have raised "paper port + live account" before the
        # strip() fix, because " DU1234567".startswith("DU") is False.
        config = _build(
            ib_settings=IBSettings(host="127.0.0.1", port=4002, account_id=" DU1234567"),
        )
        assert config is not None

    def test_whitespace_padded_live_account_classified_correctly(self) -> None:
        """Symmetric P2 fix verification for live accounts."""
        config = _build(
            ib_settings=IBSettings(host="127.0.0.1", port=4001, account_id="U1234567 "),
        )
        assert config is not None

    def test_exec_client_receives_stripped_account_id(self) -> None:
        """Codex batch 3 P2 fix: normalization must reach the exec
        client, not just the validator. Before the fix the validator
        stripped whitespace for classification but the exec client got
        the raw ``ib_settings.account_id`` — IB Gateway would then fail
        the account match on connect. This guards the full end-to-end
        path from IBSettings → normalized_account_id → exec client."""
        config = _build(
            ib_settings=IBSettings(host="127.0.0.1", port=4002, account_id=" DU1234567 "),
        )
        exec_client = config.exec_clients["INTERACTIVE_BROKERS"]
        # Nautilus may prefix the account_id with the account-type tag
        # internally, so assert the stripped id is present AND the raw
        # padded form is not.
        assert "DU1234567" in exec_client.account_id
        assert " DU1234567 " not in exec_client.account_id
