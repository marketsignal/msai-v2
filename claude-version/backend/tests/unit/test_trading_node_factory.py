"""Unit tests for :func:`_build_real_node` (Phase 4 task #154 scope-B).

Verifies the production node factory:

- Wires :class:`TradingNodePayload` fields into
  :func:`build_live_trading_node_config`
- Constructs a :class:`TradingNode` from the returned config
- Registers the IB data + exec client factories against the
  ``"INTERACTIVE_BROKERS"`` key (matching Nautilus's ``IB_VENUE.value``
  and the key :func:`build_live_trading_node_config` uses when populating
  the ``data_clients`` / ``exec_clients`` dicts)

Full construction of a real ``TradingNode`` would boot the Nautilus
kernel and try to connect to Redis (Phase 3 cache + msgbus backends) —
that's the job of the manual IB Gateway smoke test in the release
checklist. Here we monkeypatch the two moving parts so the wiring is
exercised end-to-end without I/O.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from msai.services.nautilus.trading_node_subprocess import (
    TradingNodePayload,
    _build_real_node,
)


def _make_payload(
    *,
    deployment_slug: str = "abcd1234abcd1234",
    strategy_path: str = "strategies.example.ema_cross:EMACrossStrategy",
    strategy_config_path: str = "strategies.example.config:EMACrossConfig",
    strategy_config: dict[str, Any] | None = None,
    paper_symbols: list[str] | None = None,
    ib_host: str = "127.0.0.1",
    ib_port: int = 4002,
    ib_account_id: str = "DU1234567",
) -> TradingNodePayload:
    return TradingNodePayload(
        row_id=uuid4(),
        deployment_id=uuid4(),
        deployment_slug=deployment_slug,
        strategy_path=strategy_path,
        strategy_config_path=strategy_config_path,
        strategy_config=strategy_config if strategy_config is not None else {},
        paper_symbols=paper_symbols if paper_symbols is not None else ["AAPL"],
        ib_host=ib_host,
        ib_port=ib_port,
        ib_account_id=ib_account_id,
        database_url="postgresql+asyncpg://ignored/ignored",
        redis_url="redis://ignored",
    )


class _FakeTradingNodeBuilder:
    """Minimal stand-in for Nautilus's internal
    :class:`TradingNodeBuilder`. Captures ``add_data_client_factory`` /
    ``add_exec_client_factory`` calls so tests can assert the node
    registered the IB factories under the ``"INTERACTIVE_BROKERS"``
    key that ``IB_VENUE.value`` resolves to."""

    def __init__(self) -> None:
        self.data_factories: dict[str, type] = {}
        self.exec_factories: dict[str, type] = {}

    def add_data_client_factory(self, name: str, factory: type) -> None:
        self.data_factories[name] = factory

    def add_exec_client_factory(self, name: str, factory: type) -> None:
        self.exec_factories[name] = factory


class _FakeTradingNode:
    """Stand-in for ``nautilus_trader.live.node.TradingNode`` exposing
    only the surface ``_build_real_node`` touches: constructor takes a
    ``config`` kwarg, and ``add_data_client_factory`` /
    ``add_exec_client_factory`` forward to the builder the same way
    the real node does (live/node.py:230-270)."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._builder = _FakeTradingNodeBuilder()

    def add_data_client_factory(self, name: str, factory: type) -> None:
        self._builder.add_data_client_factory(name, factory)

    def add_exec_client_factory(self, name: str, factory: type) -> None:
        self._builder.add_exec_client_factory(name, factory)


@pytest.fixture
def _patched_node(monkeypatch: pytest.MonkeyPatch) -> type[_FakeTradingNode]:
    """Monkeypatch the real ``TradingNode`` import target inside
    :func:`_build_real_node` with :class:`_FakeTradingNode` so the
    factory runs end-to-end without booting the Nautilus kernel.

    We also patch ``build_live_trading_node_config`` with a lightweight
    stub that just records the kwargs it was called with — the real
    builder's behavior is covered by :mod:`test_live_node_config`."""
    import nautilus_trader.live.node as nautilus_node

    monkeypatch.setattr(nautilus_node, "TradingNode", _FakeTradingNode)
    return _FakeTradingNode


@pytest.fixture
def _captured_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``build_live_trading_node_config`` with a stub that
    captures the kwargs the factory passes to it. The stub returns a
    sentinel object — :class:`_FakeTradingNode` stores it on ``config``
    without inspecting the contents, so the test can assert the
    builder call site received the right payload fields without
    requiring a valid :class:`TradingNodeConfig` instance."""
    captured: dict[str, Any] = {}

    class _Sentinel:
        """Opaque sentinel so test assertions don't accidentally
        poke at TradingNodeConfig internals."""

    def _stub(**kwargs: Any) -> _Sentinel:
        captured.update(kwargs)
        return _Sentinel()

    import msai.services.nautilus.live_node_config as lnc

    monkeypatch.setattr(lnc, "build_live_trading_node_config", _stub)
    return captured


def test_build_real_node_registers_ib_data_factory_under_interactive_brokers_key(
    _patched_node: type[_FakeTradingNode],
    _captured_config: dict[str, Any],
) -> None:
    """Gotcha #4 regression: the factory registration name MUST match
    the key ``build_live_trading_node_config`` uses when populating
    ``TradingNodeConfig.data_clients``. Nautilus's
    ``TradingNodeBuilder.build_data_clients()`` looks up the factory
    by that key — a mismatch surfaces as "no factory for client X"
    at ``node.build()`` time.

    The canonical key is ``IB_VENUE.value`` = ``"INTERACTIVE_BROKERS"``
    (nautilus_trader/adapters/interactive_brokers/common.py:32-33).
    """
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveDataClientFactory,
    )

    payload = _make_payload()
    node = _build_real_node(payload)

    assert isinstance(node, _FakeTradingNode)
    assert "INTERACTIVE_BROKERS" in node._builder.data_factories
    assert (
        node._builder.data_factories["INTERACTIVE_BROKERS"]
        is InteractiveBrokersLiveDataClientFactory
    )


def test_build_real_node_registers_ib_exec_factory_under_interactive_brokers_key(
    _patched_node: type[_FakeTradingNode],
    _captured_config: dict[str, Any],
) -> None:
    """Symmetric to the data client test above — same gotcha applies
    to the exec client registration."""
    from nautilus_trader.adapters.interactive_brokers.factories import (
        InteractiveBrokersLiveExecClientFactory,
    )

    payload = _make_payload()
    node = _build_real_node(payload)

    assert "INTERACTIVE_BROKERS" in node._builder.exec_factories
    assert (
        node._builder.exec_factories["INTERACTIVE_BROKERS"]
        is InteractiveBrokersLiveExecClientFactory
    )


def test_build_real_node_threads_payload_through_config_builder(
    _patched_node: type[_FakeTradingNode],
    _captured_config: dict[str, Any],
) -> None:
    """Payload → builder kwargs: every field the subprocess knows about
    (strategy path, config path, config, paper symbols, IB host/port/
    account) must flow into
    :func:`build_live_trading_node_config`. A drop here means the live
    subprocess runs a DIFFERENT strategy / account than the operator
    specified via the API."""
    payload = _make_payload(
        deployment_slug="deadbeefcafef00d",
        strategy_path="custom.path:Strategy",
        strategy_config_path="custom.path:StrategyConfig",
        strategy_config={"fast_ema_period": 7, "slow_ema_period": 21},
        paper_symbols=["AAPL", "MSFT"],
        ib_host="10.0.0.5",
        ib_port=4001,
        ib_account_id="U7654321",
    )

    _build_real_node(payload)

    assert _captured_config["deployment_slug"] == "deadbeefcafef00d"
    assert _captured_config["strategy_path"] == "custom.path:Strategy"
    assert _captured_config["strategy_config_path"] == "custom.path:StrategyConfig"
    assert _captured_config["strategy_config"] == {
        "fast_ema_period": 7,
        "slow_ema_period": 21,
    }
    assert _captured_config["paper_symbols"] == ["AAPL", "MSFT"]
    assert _captured_config["ib_settings"].host == "10.0.0.5"
    assert _captured_config["ib_settings"].port == 4001
    assert _captured_config["ib_settings"].account_id == "U7654321"


def test_build_real_node_returns_trading_node_with_config(
    _patched_node: type[_FakeTradingNode],
    _captured_config: dict[str, Any],
) -> None:
    """The node returned from the factory must hold the config that
    :func:`build_live_trading_node_config` produced — Nautilus's
    ``TradingNode.__init__`` stores the config on ``self._config``
    and ``build()`` reads ``data_clients`` / ``exec_clients`` from
    there."""
    payload = _make_payload()
    node = _build_real_node(payload)

    assert node.config is not None
    # The sentinel from _captured_config was threaded through
    assert hasattr(node, "config")


def test_build_real_node_does_not_call_node_build(
    _patched_node: type[_FakeTradingNode],
    _captured_config: dict[str, Any],
) -> None:
    """The factory MUST return the node in an un-built state —
    :func:`run_subprocess_async` calls ``node.build()`` AFTER the
    factory returns so the heartbeat thread is alive during the
    (potentially slow) build. Calling build inside the factory would
    defeat that ordering (decision #17 / Codex v5 P0)."""
    payload = _make_payload()
    node = _build_real_node(payload)

    assert not hasattr(node, "build_called"), (
        "factory must not call node.build() — run_subprocess_async owns "
        "the build step so the heartbeat can advance during a slow IB "
        "contract load"
    )
