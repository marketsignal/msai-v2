"""Shared unit-test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_ib_factory_globals():
    """Clear Nautilus IB adapter factory globals between tests.

    Rationale (research brief finding #3): Nautilus 1.223.0 caches
    clients/providers in module-level dicts that have no ``.clear()``
    helper. Between unit tests that touch ``get_cached_ib_client`` or
    ``get_cached_interactive_brokers_instrument_provider``, a stale
    cached client from an earlier test can leak into a later one.
    Production is unaffected because each ``msai instruments refresh``
    invocation is a fresh process.

    We don't clear ``GATEWAYS`` because that dict is only populated
    when ``dockerized_gateway=...`` is passed to
    ``get_cached_ib_client`` — the CLI never does, so the dict stays
    empty in our test paths.

    Runs on every unit test (autouse) but the clear is cheap: the
    dicts are empty when untouched.
    """
    yield
    try:
        from nautilus_trader.adapters.interactive_brokers import factories
    except ImportError:
        # Running without Nautilus installed (some CI jobs skip heavy
        # deps). No globals to clear.
        return
    factories.IB_CLIENTS.clear()
    factories.IB_INSTRUMENT_PROVIDERS.clear()
