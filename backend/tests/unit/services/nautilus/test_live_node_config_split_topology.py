"""Unit tests for the per-account split-topology TradingNodeConfig builder.

PR 1 Task T11 (multi-account-broker-fleet). The builder under test —
:func:`msai.services.nautilus.live_node_config.build_per_account_trading_node_config` —
produces a ``TradingNodeConfig`` whose ``data_clients`` contains ONLY
the Databento client and whose ``exec_clients`` contains ONLY the IB
exec client. The :class:`SymbologyShimActor` lands in the ``actors``
list with the canonical→native bar-type map injected (without which
``on_start`` has no native subscriptions to fire — Codex iter 7/8 P1).

These tests stay SYNC because the builder is sync (the subprocess at
``trading_node_subprocess.py:675`` calls ``node_factory(payload)``
synchronously — no DB session, no await). Pre-resolved native ids +
venue_dataset_map are passed straight in.
"""

from __future__ import annotations

import pytest

from msai.services.nautilus.live_node_config import (
    IB_VENUE,
    build_per_account_trading_node_config,
)

# ---------------------------------------------------------------------------
# Test data — kept at module scope so each test can mix-and-match.
# ---------------------------------------------------------------------------

_NATIVE_IDS: list[str] = ["AAPL.XNAS"]
_VENUE_DATASET_MAP: dict[str, str] = {"XNAS": "DBEQ.BASIC"}
_BAR_TYPE_MAP: dict[str, str] = {
    "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
}


def _build(
    *,
    account_id: str = "DUP733214",
    ibg_client_id: int = 210,
) -> object:
    """Build a config with sensible defaults for the topology assertions."""
    return build_per_account_trading_node_config(
        account_id=account_id,
        ibg_client_id=ibg_client_id,
        ib_login_key="marin1016test",
        native_instrument_ids=_NATIVE_IDS,
        venue_dataset_map=_VENUE_DATASET_MAP,
        canonical_to_native_bar_types=_BAR_TYPE_MAP,
        databento_api_key="dbn-test-key",
        ib_host="ib-gateway",
        ib_port=4002,
    )


# ---------------------------------------------------------------------------
# data_clients shape — Databento ONLY (no IB data client).
# ---------------------------------------------------------------------------


def test_data_clients_contains_only_databento() -> None:
    """``data_clients`` must contain exactly one entry — the Databento
    client keyed by ``str(DATABENTO_CLIENT_ID)``. No IB data client."""
    from nautilus_trader.adapters.databento.constants import DATABENTO_CLIENT_ID

    config = _build()
    assert set(config.data_clients.keys()) == {str(DATABENTO_CLIENT_ID)}
    # Belt-and-braces: confirm IB is NOT in data_clients (the legacy
    # builders DO wire it; the per-account builder MUST drop it).
    assert IB_VENUE.value not in config.data_clients


def test_data_client_carries_native_instrument_ids() -> None:
    """The Databento client config must carry the pre-resolved native
    ids in NATIVE venue form (e.g. ``AAPL.XNAS``)."""
    from nautilus_trader.adapters.databento.constants import DATABENTO_CLIENT_ID

    config = _build()
    dbn_cfg = config.data_clients[str(DATABENTO_CLIENT_ID)]
    native_ids = {str(iid) for iid in (dbn_cfg.instrument_ids or [])}
    assert "AAPL.XNAS" in native_ids


def test_data_client_carries_venue_dataset_map() -> None:
    """The Databento client config must carry the authoritative
    venue_dataset_map so Nautilus uses our dataset choice rather than
    the publisher-default lookup (Codex iter 4 P2-2 of T10)."""
    from nautilus_trader.adapters.databento.constants import DATABENTO_CLIENT_ID

    config = _build()
    dbn_cfg = config.data_clients[str(DATABENTO_CLIENT_ID)]
    assert dbn_cfg.venue_dataset_map == _VENUE_DATASET_MAP


# ---------------------------------------------------------------------------
# exec_clients shape — IB exec ONLY (with the per-account account_id +
# ibg_client_id).
# ---------------------------------------------------------------------------


def test_exec_clients_contains_only_ib_with_account_id() -> None:
    """``exec_clients`` must contain exactly one entry — the IB exec
    client keyed under ``IB_VENUE.value`` ("INTERACTIVE_BROKERS") —
    and the config must carry the per-account ``account_id`` +
    ``ibg_client_id``."""
    config = _build(account_id="DUP733215", ibg_client_id=215)
    assert set(config.exec_clients.keys()) == {IB_VENUE.value}

    ib_cfg = config.exec_clients[IB_VENUE.value]
    # ``account_id`` is normalized by stripping whitespace; the
    # underlying field equals the bare account id passed in.
    assert "DUP733215" in str(ib_cfg.account_id)
    assert ib_cfg.ibg_client_id == 215


def test_two_accounts_get_distinct_ibg_client_ids() -> None:
    """Two different deployments (different account ids + different
    ibg_client_ids passed in by the supervisor) must NOT collide on
    the IB exec client_id slot (gotcha #3)."""
    cfg_a = _build(account_id="DUP733214", ibg_client_id=214)
    cfg_b = _build(account_id="DUP733215", ibg_client_id=215)
    assert (
        cfg_a.exec_clients[IB_VENUE.value].ibg_client_id
        != cfg_b.exec_clients[IB_VENUE.value].ibg_client_id
    )


# ---------------------------------------------------------------------------
# SymbologyShimActor wire-up (Codex iter 6 P1-2 + iter 7 P1 + iter 8 P1)
# ---------------------------------------------------------------------------


def test_symbology_shim_actor_is_wired() -> None:
    """``TradingNodeConfig.actors`` must include the
    ``SymbologyShimActor`` :class:`ImportableActorConfig` with BOTH
    ``canonical_to_native_bar_types`` (load-bearing — without it,
    ``on_start`` has nothing to subscribe to) AND ``venue_dataset_map``
    (used by the inbound audit path) injected into ``config``.

    Assert against the importable shape — ``actor_path`` / ``config_path``
    / ``config`` — NOT against a raw actor instance (Codex iter 6 P1-2)."""
    config = _build()
    actor_paths = [a.actor_path for a in config.actors]
    assert any("SymbologyShimActor" in p for p in actor_paths), (
        f"SymbologyShimActor missing from actors; got actor_paths={actor_paths!r}"
    )

    shim_entry = next(a for a in config.actors if "SymbologyShimActor" in a.actor_path)
    assert shim_entry.actor_path == "msai.services.symbology_shim_actor:SymbologyShimActor"
    assert shim_entry.config_path == "msai.services.symbology_shim_actor:SymbologyShimActorConfig"

    shim_cfg = shim_entry.config
    assert shim_cfg["canonical_to_native_bar_types"] == _BAR_TYPE_MAP
    assert shim_cfg["venue_dataset_map"] == _VENUE_DATASET_MAP


# ---------------------------------------------------------------------------
# Validation — port/account consistency (gotcha #6) + non-empty account_id.
# ---------------------------------------------------------------------------


def test_empty_account_id_raises() -> None:
    """An empty ``account_id`` is a configuration bug (the supervisor
    should have resolved the account from the broker-account registry
    before reaching the builder). Fail fast."""
    with pytest.raises(ValueError, match="account_id"):
        build_per_account_trading_node_config(
            account_id="",
            ibg_client_id=210,
            ib_login_key="k",
            native_instrument_ids=_NATIVE_IDS,
            venue_dataset_map=_VENUE_DATASET_MAP,
            canonical_to_native_bar_types=_BAR_TYPE_MAP,
            databento_api_key="k",
            ib_host="ib-gateway",
            ib_port=4002,
        )


def test_paper_port_with_live_account_raises() -> None:
    """Gotcha #6: port 4002 (paper) with a live account id is a silent
    data-flow killer. The shared port-account validator must fire."""
    with pytest.raises(ValueError):
        build_per_account_trading_node_config(
            account_id="U1234567",  # live (no DU prefix)
            ibg_client_id=210,
            ib_login_key="k",
            native_instrument_ids=_NATIVE_IDS,
            venue_dataset_map=_VENUE_DATASET_MAP,
            canonical_to_native_bar_types=_BAR_TYPE_MAP,
            databento_api_key="k",
            ib_host="ib-gateway",
            ib_port=4002,  # paper port
        )


# ---------------------------------------------------------------------------
# F2 (Codex iter 2 P1) — IB exec instrument provider preload uses
# ``load_contracts`` with LISTING-venue IBContracts, NOT ``load_ids`` with
# unresolvable ``.IBKR`` ids.
# ---------------------------------------------------------------------------


def _make_resolved_equity(
    *,
    canonical_id: str = "AAPL.IBKR",
    listing_venue: str = "NASDAQ",
    symbol: str = "AAPL",
):  # type: ignore[no-untyped-def]
    """Build a fixture ResolvedInstrument with an equity contract_spec
    keyed by the LISTING venue (NASDAQ/NYSE), matching what
    ``lookup_for_live`` produces in production."""
    from datetime import date

    from msai.services.nautilus.security_master.live_resolver import (
        AssetClass,
        ResolvedInstrument,
    )

    return ResolvedInstrument(
        canonical_id=canonical_id,
        asset_class=AssetClass.EQUITY,
        contract_spec={
            "secType": "STK",
            "symbol": symbol,
            "exchange": "SMART",
            "primaryExchange": listing_venue,
            "currency": "USD",
        },
        effective_window=(date(2024, 1, 1), None),
    )


def test_ib_exec_provider_uses_load_contracts_with_listing_venue() -> None:
    """F2 fix: the IB exec instrument provider config MUST preload via
    ``load_contracts`` populated with ``IBContract`` objects whose
    ``primaryExchange`` is the LISTING venue (NASDAQ/NYSE/etc.), NOT
    ``load_ids`` keyed by the canonical ``.IBKR`` venue.

    Under ``SymbologyMethod.IB_SIMPLIFIED`` the InstrumentId.venue is
    interpreted as IB's exchange/primaryExchange during contract
    qualification — ``IBKR`` is NOT a valid IB exchange/MIC so the
    prior ``load_ids`` path silently failed and left the first per-
    account equity order without a cached contract. Mirror the legacy
    portfolio builder's pattern via ``load_contracts``.
    """
    from nautilus_trader.adapters.interactive_brokers.config import (
        InteractiveBrokersInstrumentProviderConfig,
    )

    resolved = [
        _make_resolved_equity(
            canonical_id="AAPL.IBKR",
            listing_venue="NASDAQ",
            symbol="AAPL",
        ),
        _make_resolved_equity(
            canonical_id="GE.IBKR",
            listing_venue="NYSE",
            symbol="GE",
        ),
    ]

    config = build_per_account_trading_node_config(
        account_id="DUP733214",
        ibg_client_id=210,
        ib_login_key="k",
        native_instrument_ids=["AAPL.XNAS", "GE.XNYS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC", "XNYS": "DBEQ.BASIC"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
            "GE.IBKR-1-MINUTE-LAST-EXTERNAL": "GE.XNYS-1-MINUTE-LAST-EXTERNAL",
        },
        databento_api_key="k",
        ib_host="ib-gateway",
        ib_port=4002,
        resolved_instruments=resolved,
    )

    ib_cfg = config.exec_clients[IB_VENUE.value]
    provider = ib_cfg.instrument_provider
    assert isinstance(provider, InteractiveBrokersInstrumentProviderConfig)

    # F2: load_ids MUST be None (or empty) — we no longer feed unresolvable
    # .IBKR ids. The preload happens via load_contracts with IBContract
    # objects keyed by the LISTING venue.
    assert not provider.load_ids, (
        f"F2 regression: load_ids must not be populated with .IBKR ids — "
        f"got {provider.load_ids!r}. Use load_contracts (LISTING venue) instead."
    )

    # The load_contracts frozenset must contain real IBContract entries
    # whose ``primaryExchange`` is the LISTING venue (NASDAQ/NYSE) — the
    # SAME venue IB Gateway uses during contract qualification.
    assert provider.load_contracts is not None
    primary_exchanges = {c.primaryExchange for c in provider.load_contracts}
    assert "NASDAQ" in primary_exchanges
    assert "NYSE" in primary_exchanges

    # Belt-and-braces: NO contract in load_contracts has primaryExchange
    # 'IBKR' (would re-introduce the F2 bug at the IBContract layer).
    assert "IBKR" not in primary_exchanges


def test_ib_exec_provider_no_ibkr_strings_anywhere() -> None:
    """F2 negative test: serialize the IB instrument provider config and
    assert the canonical ``.IBKR`` venue suffix does NOT appear anywhere
    in the IBContract preload. This guards against a future regression
    that re-introduces ``.IBKR`` via a new code path."""
    resolved = [
        _make_resolved_equity(canonical_id="AAPL.IBKR", listing_venue="NASDAQ"),
    ]
    config = build_per_account_trading_node_config(
        account_id="DUP733214",
        ibg_client_id=210,
        ib_login_key="k",
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        databento_api_key="k",
        ib_host="ib-gateway",
        ib_port=4002,
        resolved_instruments=resolved,
    )
    provider = config.exec_clients[IB_VENUE.value].instrument_provider
    for contract in provider.load_contracts or ():  # type: ignore[union-attr]
        assert contract.primaryExchange != "IBKR"
        assert contract.exchange != "IBKR"


def test_ib_exec_provider_load_contracts_empty_when_no_resolved() -> None:
    """When ``resolved_instruments`` is omitted (test fixtures that
    exercise the topology shape without resolver output), the provider
    falls back to an empty ``load_contracts``. Production callers in the
    subprocess always thread resolved instruments through from the
    supervisor; this branch exists for low-level topology tests."""
    from nautilus_trader.adapters.interactive_brokers.config import (
        InteractiveBrokersInstrumentProviderConfig,
    )

    config = build_per_account_trading_node_config(
        account_id="DUP733214",
        ibg_client_id=210,
        ib_login_key="k",
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        databento_api_key="k",
        ib_host="ib-gateway",
        ib_port=4002,
    )
    provider = config.exec_clients[IB_VENUE.value].instrument_provider
    assert isinstance(provider, InteractiveBrokersInstrumentProviderConfig)
    # Empty resolved_instruments → empty load_contracts (frozenset)
    assert not provider.load_contracts


# ---------------------------------------------------------------------------
# Codex iter 1 P1-1 + P1-3 — strategy threading + bar_type rewrite.
# ---------------------------------------------------------------------------


def test_build_per_account_strategy_configs_rewrites_bar_type_to_ibkr() -> None:
    """Codex iter 3 F1: ``build_per_account_strategy_configs`` MUST
    rewrite each member's ``bar_type`` to ``<SYM>.IBKR-...`` (so the
    strategy subscribes onto the SAME topic the SymbologyShimActor
    republishes to). The ``instrument_id`` MUST stay on the LISTING
    venue (``AAPL.NASDAQ``) so order routing resolves cleanly against
    the IB exec provider's preloaded contract cache. Without the
    bar_type rewrite no bars reach the strategy; without the
    instrument_id staying on the listing venue, orders fail
    contract qualification at submit time."""
    from uuid import uuid4

    from msai.services.nautilus.live_node_config import (
        build_per_account_strategy_configs,
    )
    from msai.services.nautilus.trading_node_subprocess import (
        StrategyMemberPayload,
    )

    member = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "fast_period": 10,
            "slow_period": 20,
        },
        strategy_id_full="EMACrossStrategy-0-abcdef0123456789",
        instruments=["AAPL"],
    )

    configs = build_per_account_strategy_configs([member], deployment_slug="abcdef0123456789")

    assert len(configs) == 1
    cfg = configs[0].config
    # F1: bar_type rewritten to .IBKR (data path canonicalized to shim's
    # republish topic) while instrument_id retains the LISTING venue
    # (exec path resolves via IB contract cache).
    assert cfg["instrument_id"] == "AAPL.NASDAQ"
    assert cfg["bar_type"] == "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL"
    # Non-venue strategy parameters survive untouched.
    assert cfg["fast_period"] == 10
    assert cfg["slow_period"] == 20
    # order_id_tag derived from strategy_id_full (legacy parity).
    assert cfg["order_id_tag"] == "0-abcdef0123456789"
    # NASDAQ listing venue triggers US-equity TIF=DAY override + manage_stop.
    assert cfg.get("manage_stop") is True


def test_build_per_account_strategy_configs_preserves_already_ibkr() -> None:
    """A member whose ``bar_type`` is already in ``.IBKR`` form (the
    supervisor's payload factory may produce this in the future) is
    left unchanged — the rewrite is idempotent."""
    from uuid import uuid4

    from msai.services.nautilus.live_node_config import (
        build_per_account_strategy_configs,
    )
    from msai.services.nautilus.trading_node_subprocess import (
        StrategyMemberPayload,
    )

    member = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={
            # ``instrument_id`` on LISTING venue (matches what the
            # supervisor's payload factory produces today). The helper
            # leaves it untouched per F1.
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
        },
        strategy_id_full="EMACrossStrategy-0-deadbeefcafef00d",
        instruments=["AAPL"],
    )

    configs = build_per_account_strategy_configs([member], deployment_slug="deadbeefcafef00d")

    cfg = configs[0].config
    # instrument_id LEFT on LISTING venue (F1 architectural decision).
    assert cfg["instrument_id"] == "AAPL.NASDAQ"
    # bar_type already in .IBKR form is idempotent.
    assert cfg["bar_type"] == "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL"


@pytest.mark.parametrize(
    ("bar_type_in", "bar_type_out"),
    [
        (
            "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
        ),
        (
            "SPY.ARCA-1-MINUTE-LAST-EXTERNAL",
            "SPY.IBKR-1-MINUTE-LAST-EXTERNAL",
        ),
        (
            "GE.NYSE-1-MINUTE-LAST-EXTERNAL",
            "GE.IBKR-1-MINUTE-LAST-EXTERNAL",
        ),
        # F3: share-class symbol ``BRK.B`` must survive — rsplit on the
        # LAST dot keeps the dotted root intact. The buggy partition()
        # version produced ``BRK.IBKR-1-MINUTE-LAST-EXTERNAL`` (lost ``B``).
        (
            "BRK.B.NYSE-1-MINUTE-LAST-EXTERNAL",
            "BRK.B.IBKR-1-MINUTE-LAST-EXTERNAL",
        ),
        (
            "BF.B.NYSE-1-MINUTE-LAST-EXTERNAL",
            "BF.B.IBKR-1-MINUTE-LAST-EXTERNAL",
        ),
        # Already in .IBKR form — idempotent rewrite.
        (
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
        ),
    ],
)
def test_rewrite_bar_type_to_ibkr_handles_share_class_and_us_venues(
    bar_type_in: str,
    bar_type_out: str,
) -> None:
    """F3: ``_rewrite_bar_type_to_ibkr`` MUST use ``rpartition('.')`` so
    share-class symbols like ``BRK.B.NYSE`` keep ``BRK.B`` as the symbol
    root — not lose the ``.B`` segment. Mirrors the
    ``_strip_venue_suffix`` rsplit fix in ``live_supervisor/__main__.py``
    so the supervisor's Databento bar-type map keys (``BRK.B.IBKR-...``)
    match what the strategy subscribes to here."""
    from msai.services.nautilus.live_node_config import (
        _rewrite_bar_type_to_ibkr,
    )

    assert _rewrite_bar_type_to_ibkr(bar_type_in) == bar_type_out


def test_rewrite_bar_type_to_ibkr_passes_through_non_venue_inputs() -> None:
    """Inputs that don't carry a ``.VENUE`` token (already-bare symbol,
    empty string, garbage) survive unchanged so the helper is safe to
    call on every member's ``bar_type`` without pre-validation."""
    from msai.services.nautilus.live_node_config import (
        _rewrite_bar_type_to_ibkr,
    )

    # Bare symbol — no dot in the prefix.
    assert _rewrite_bar_type_to_ibkr("AAPL-1-MINUTE-LAST-EXTERNAL") == (
        "AAPL-1-MINUTE-LAST-EXTERNAL"
    )
    # Empty pass-through.
    assert _rewrite_bar_type_to_ibkr("") == ""


def test_build_per_account_strategy_configs_preserves_brk_b_symbol_in_instrument_id() -> None:
    """End-to-end F1 + F3: a ``BRK.B`` share-class strategy must end up
    with ``instrument_id=BRK.B.NYSE`` (listing venue intact, share-class
    suffix intact) AND ``bar_type=BRK.B.IBKR-...`` (data path
    canonicalized for the shim's republish topic). Regression guard
    against the partition('.') bug that would have produced
    ``BRK.IBKR-...`` (truncated symbol) and ``BRK.NYSE`` (truncated
    symbol with surviving venue) under the old helper."""
    from uuid import uuid4

    from msai.services.nautilus.live_node_config import (
        build_per_account_strategy_configs,
    )
    from msai.services.nautilus.trading_node_subprocess import (
        StrategyMemberPayload,
    )

    member = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={
            "instrument_id": "BRK.B.NYSE",
            "bar_type": "BRK.B.NYSE-1-MINUTE-LAST-EXTERNAL",
        },
        strategy_id_full="EMACrossStrategy-0-abcdef0123456789",
        instruments=["BRK.B"],
    )

    configs = build_per_account_strategy_configs([member], deployment_slug="abcdef0123456789")

    cfg = configs[0].config
    # F1: instrument_id LEFT on listing venue → IB exec contract cache
    # resolves it via primaryExchange=NYSE.
    assert cfg["instrument_id"] == "BRK.B.NYSE"
    # F1 + F3: bar_type rewritten to .IBKR with the dotted share-class
    # root preserved — matches the supervisor's republish topic key.
    assert cfg["bar_type"] == "BRK.B.IBKR-1-MINUTE-LAST-EXTERNAL"


def test_per_account_config_threads_strategies_from_subprocess_call() -> None:
    """Codex iter 1 P1-1: when ``strategies=`` is passed in, the
    returned ``TradingNodeConfig.strategies`` MUST contain those
    configs — NOT an empty list. The subprocess relies on this to
    spawn a TradingNode that actually subscribes + trades."""
    from nautilus_trader.config import ImportableStrategyConfig

    fake_strategy = ImportableStrategyConfig(
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        config_path="strategies.example.config:EMACrossConfig",
        config={"instrument_id": "AAPL.IBKR", "order_id_tag": "0-abc"},
    )

    config = build_per_account_trading_node_config(
        account_id="DUP733214",
        ibg_client_id=210,
        ib_login_key="k",
        native_instrument_ids=_NATIVE_IDS,
        venue_dataset_map=_VENUE_DATASET_MAP,
        canonical_to_native_bar_types=_BAR_TYPE_MAP,
        databento_api_key="k",
        ib_host="ib-gateway",
        ib_port=4002,
        strategies=[fake_strategy],
    )

    assert len(config.strategies) == 1
    assert config.strategies[0].strategy_path == fake_strategy.strategy_path
