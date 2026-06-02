"""Routing tests for the SYNC ``_build_real_node`` factory in
``trading_node_subprocess.py``.

PR 1 Task T12 (multi-account-broker-fleet). When the supervisor's
ASYNC payload factory pre-resolved the per-account topology fields
(``native_instrument_ids`` + ``venue_dataset_map`` +
``canonical_to_native_bar_types``), ``_build_real_node`` MUST route
to :func:`build_per_account_trading_node_config` (Databento data + IB
exec). When any of those fields is empty, the factory falls back to
the legacy :func:`build_portfolio_trading_node_config` so existing
deployments keep spawning.

The tests exercise ``_build_real_node`` directly (sync function) and
assert on the produced ``TradingNodeConfig``'s actor + data-client
shape — without standing up a real Nautilus ``TradingNode`` (which
would require IB / Databento bindings).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from msai.services.nautilus.trading_node_subprocess import (
    StrategyMemberPayload,
    TradingNodePayload,
    _build_real_node,
)


def _make_payload(
    *,
    native_instrument_ids: list[str] | None = None,
    venue_dataset_map: dict[str, str] | None = None,
    canonical_to_native_bar_types: dict[str, str] | None = None,
    ibg_client_id: int = 0,
) -> TradingNodePayload:
    """Build a payload with sensible defaults for the routing test."""
    member = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={
            "instrument_id": "AAPL.IBKR",
            "bar_type": "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL",
        },
        strategy_code_hash="",
        strategy_id_full="EMACrossStrategy-0-abcdef0123456789",
        instruments=["AAPL"],
        resolved_instruments=(),
    )
    return TradingNodePayload(
        row_id=uuid4(),
        deployment_id=uuid4(),
        deployment_slug="abcdef0123456789",
        strategy_path=member.strategy_path,
        strategy_config_path=member.strategy_config_path,
        strategy_config=member.strategy_config,
        strategy_id=member.strategy_id,
        paper_symbols=["AAPL"],
        canonical_instruments=["AAPL.NASDAQ"],
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_account_id="DUP733214",
        strategy_members=[member],
        ibg_client_id=ibg_client_id,
        native_instrument_ids=native_instrument_ids or [],
        venue_dataset_map=venue_dataset_map or {},
        canonical_to_native_bar_types=canonical_to_native_bar_types or {},
    )


def test_payload_with_per_account_fields_uses_databento_builder() -> None:
    """When ``use_per_account_topology`` is True, ``_build_real_node``
    MUST construct the per-account ``TradingNodeConfig`` whose
    ``data_clients`` is keyed by ``DATABENTO`` and whose ``actors``
    list carries the ``SymbologyShimActor`` with the canonical→native
    bar-type map injected.

    The TradingNode constructor and client-factory registration are
    side-effects that require IB / Databento bindings — patch them
    out so we can inspect the config that was passed in."""
    payload = _make_payload(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        ibg_client_id=12345,
    )

    config_box: list[Any] = []
    data_factories: list[str] = []
    exec_factories: list[str] = []

    class _FakeNode:
        def __init__(self, config: Any) -> None:  # noqa: D401 — test stub
            config_box.append(config)

        def add_data_client_factory(self, name: str, factory: Any) -> None:
            data_factories.append(name)

        def add_exec_client_factory(self, name: str, factory: Any) -> None:
            exec_factories.append(name)

    with patch(
        "nautilus_trader.live.node.TradingNode",
        new=_FakeNode,
    ):
        _build_real_node(payload)

    config = config_box[0]
    # Importable-actor shape (kernel ActorFactory.create deserializes the dict).
    from nautilus_trader.adapters.databento.constants import DATABENTO_CLIENT_ID

    assert str(DATABENTO_CLIENT_ID) in config.data_clients
    actor_paths = [a.actor_path for a in config.actors]
    assert any("SymbologyShimActor" in p for p in actor_paths)

    shim_entry = next(a for a in config.actors if "SymbologyShimActor" in a.actor_path)
    # canonical_to_native_bar_types is wired into the ImportableActorConfig.
    assert shim_entry.config["canonical_to_native_bar_types"] == {
        "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
    }

    # Both client factories were registered.
    assert "DATABENTO" in data_factories
    assert "INTERACTIVE_BROKERS" in exec_factories


def test_payload_with_per_account_fields_propagates_ibg_client_id() -> None:
    """The exec client must carry the ``ibg_client_id`` value the
    supervisor allocated. Without this, two concurrent accounts could
    collide on IB Gateway's client_id slot (gotcha #3)."""
    payload = _make_payload(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
        ibg_client_id=54321,
    )

    config_box: list[Any] = []

    class _FakeNode:
        def __init__(self, config: Any) -> None:
            config_box.append(config)

        def add_data_client_factory(self, name: str, factory: Any) -> None:
            pass

        def add_exec_client_factory(self, name: str, factory: Any) -> None:
            pass

    with patch(
        "nautilus_trader.live.node.TradingNode",
        new=_FakeNode,
    ):
        _build_real_node(payload)

    from nautilus_trader.adapters.interactive_brokers.common import IB_VENUE

    config = config_box[0]
    exec_cfg = config.exec_clients[IB_VENUE.value]
    assert exec_cfg.ibg_client_id == 54321


def test_payload_without_per_account_fields_falls_back_to_legacy_builder() -> None:
    """When ``use_per_account_topology`` is False (any of the three
    pre-resolved fields is empty), ``_build_real_node`` MUST route to
    the legacy :func:`build_portfolio_trading_node_config` — IB owns
    both data + exec, no Databento client, no SymbologyShimActor."""
    payload = _make_payload()  # no per-account fields → False

    assert payload.use_per_account_topology is False

    # The legacy portfolio builder requires non-empty resolved_instruments
    # to build the IB provider config. Patch it out + TradingNode + the
    # factory-registration calls so we can inspect the routing decision
    # without booting Nautilus.
    legacy_called: list[bool] = []
    per_account_called: list[bool] = []
    data_factories: list[str] = []
    exec_factories: list[str] = []

    def _fake_legacy_builder(**kwargs: Any) -> Any:
        legacy_called.append(True)
        return object()  # opaque sentinel — IB-routed code path

    def _fake_per_account_builder(**kwargs: Any) -> Any:  # noqa: ARG001
        per_account_called.append(True)
        return object()

    class _FakeNode:
        def __init__(self, config: Any) -> None:
            pass

        def add_data_client_factory(self, name: str, factory: Any) -> None:
            data_factories.append(name)

        def add_exec_client_factory(self, name: str, factory: Any) -> None:
            exec_factories.append(name)

    with (
        patch(
            "msai.services.nautilus.live_node_config.build_portfolio_trading_node_config",
            new=_fake_legacy_builder,
        ),
        patch(
            "msai.services.nautilus.live_node_config.build_per_account_trading_node_config",
            new=_fake_per_account_builder,
        ),
        patch(
            "nautilus_trader.live.node.TradingNode",
            new=_FakeNode,
        ),
    ):
        _build_real_node(payload)

    # Legacy builder ran; per-account builder did NOT.
    assert legacy_called == [True]
    assert per_account_called == []

    # IB factory was registered for BOTH data and exec.
    assert data_factories == ["INTERACTIVE_BROKERS"]
    assert exec_factories == ["INTERACTIVE_BROKERS"]


def test_payload_missing_bar_type_map_falls_back_to_legacy() -> None:
    """``canonical_to_native_bar_types`` is THE load-bearing field —
    without it, the SymbologyShimActor's ``on_start`` has nothing to
    subscribe to and bars never reach the strategy. Treat an empty
    map as "not populated" and route to the legacy builder so the
    failure mode is loud (legacy IB data client) rather than silent
    (no bars)."""
    payload = _make_payload(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
        canonical_to_native_bar_types={},  # EMPTY — must NOT route per-account
    )
    assert payload.use_per_account_topology is False


@pytest.mark.parametrize(
    ("native_ids", "venue_map", "bar_map", "expected"),
    [
        ([], {}, {}, False),
        (["AAPL.XNAS"], {}, {}, False),
        ([], {"XNAS": "EQUS.MINI"}, {}, False),
        ([], {}, {"k": "v"}, False),
        (["AAPL.XNAS"], {"XNAS": "EQUS.MINI"}, {}, False),
        (["AAPL.XNAS"], {}, {"k": "v"}, False),
        ([], {"XNAS": "EQUS.MINI"}, {"k": "v"}, False),
        (["AAPL.XNAS"], {"XNAS": "EQUS.MINI"}, {"k": "v"}, True),
    ],
)
def test_use_per_account_topology_requires_all_three_fields(
    native_ids: list[str],
    venue_map: dict[str, str],
    bar_map: dict[str, str],
    expected: bool,
) -> None:
    """The routing predicate is the conjunction of all three fields
    being non-empty. Any one empty MUST fall back to the legacy
    builder."""
    payload = _make_payload(
        native_instrument_ids=native_ids,
        venue_dataset_map=venue_map,
        canonical_to_native_bar_types=bar_map,
    )
    assert payload.use_per_account_topology is expected


def test_per_account_routing_threads_strategies_through_builder() -> None:
    """Codex iter 1 P1-1 + P1-3 of PR 1: when routing to
    ``build_per_account_trading_node_config``, the subprocess MUST
    build ``ImportableStrategyConfig``s from ``payload.strategy_members``
    AND pass them as ``strategies=`` to the builder. Without this, the
    spawned TradingNode has zero strategies and the deployment never
    subscribes or trades. Per the same fix, the strategy configs MUST
    carry the canonical ``.IBKR`` venue so the SymbologyShimActor's
    republish topic matches.
    """
    # Member starts at the listing venue (.NASDAQ) — the supervisor's
    # payload factory hasn't rewritten it yet. The subprocess routing
    # path is responsible for the rewrite via
    # ``build_per_account_strategy_configs``.
    member = StrategyMemberPayload(
        strategy_id=uuid4(),
        strategy_path="strategies.example.ema_cross:EMACrossStrategy",
        strategy_config_path="strategies.example.config:EMACrossConfig",
        strategy_config={
            "instrument_id": "AAPL.NASDAQ",
            "bar_type": "AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL",
        },
        strategy_id_full="EMACrossStrategy-0-abcdef0123456789",
        instruments=["AAPL"],
    )
    payload = TradingNodePayload(
        row_id=uuid4(),
        deployment_id=uuid4(),
        deployment_slug="abcdef0123456789",
        strategy_path=member.strategy_path,
        strategy_config_path=member.strategy_config_path,
        strategy_config=member.strategy_config,
        strategy_id=member.strategy_id,
        paper_symbols=["AAPL"],
        canonical_instruments=["AAPL.NASDAQ"],
        ib_host="127.0.0.1",
        ib_port=4002,
        ib_account_id="DUP733214",
        strategy_members=[member],
        ibg_client_id=12345,
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "EQUS.MINI"},
        canonical_to_native_bar_types={
            "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL": "AAPL.XNAS-1-MINUTE-LAST-EXTERNAL",
        },
    )

    config_box: list[Any] = []

    class _FakeNode:
        def __init__(self, config: Any) -> None:
            config_box.append(config)

        def add_data_client_factory(self, name: str, factory: Any) -> None:
            pass

        def add_exec_client_factory(self, name: str, factory: Any) -> None:
            pass

    with patch("nautilus_trader.live.node.TradingNode", new=_FakeNode):
        _build_real_node(payload)

    config = config_box[0]
    # P1-1 — strategies list is non-empty (one entry per member).
    assert len(config.strategies) == 1
    strategy_cfg = config.strategies[0].config
    # Codex iter 3 F1 — ONLY ``bar_type`` is rewritten to ``.IBKR`` so the
    # strategy subscribes onto the SymbologyShimActor's republish topic.
    # ``instrument_id`` stays on the LISTING venue so order routing
    # resolves cleanly against the IB exec provider's preloaded contracts
    # (which are keyed by primaryExchange = NASDAQ/NYSE/etc.).
    assert strategy_cfg["instrument_id"] == "AAPL.NASDAQ"
    assert strategy_cfg["bar_type"] == "AAPL.IBKR-1-MINUTE-LAST-EXTERNAL"
    # ``order_id_tag`` derived from strategy_id_full (legacy parity).
    assert strategy_cfg["order_id_tag"] == "0-abcdef0123456789"
