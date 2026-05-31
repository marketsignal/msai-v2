"""Tests for the Databento live data-client config split builder (PR 1 T10).

The async resolver (DB-backed) is covered by an integration test in
``backend/tests/integration/services/test_symbology_shim_resolve.py`` (which
is what powers ``resolve_databento_targets`` underneath). These unit tests
exercise the SYNC builder path that runs in the TradingNode subprocess —
pure-function, no fixtures required.
"""

from __future__ import annotations

import pytest

from msai.services.nautilus.databento_live_config import (
    ResolvedDatabentoTargets,
    build_databento_data_client_config,
)


def test_resolved_targets_dataclass_is_frozen() -> None:
    targets = ResolvedDatabentoTargets(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
    )
    with pytest.raises((AttributeError, Exception)):
        targets.native_instrument_ids = ["bogus"]  # type: ignore[misc]


def test_builder_pins_reconnect_timeout_to_10_min() -> None:
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS", "SPY.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="dbn-test-key",
    )
    assert config.reconnect_timeout_mins == 10


def test_builder_pre_populates_native_instrument_ids() -> None:
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS", "SPY.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="dbn-test-key",
    )
    native_ids = {str(iid) for iid in (config.instrument_ids or [])}
    assert "AAPL.XNAS" in native_ids
    assert "SPY.XNAS" in native_ids


def test_builder_carries_authoritative_venue_dataset_map() -> None:
    # Codex iter 4 P2-2: builder MUST populate venue_dataset_map so Nautilus
    # uses our authoritative dataset choice rather than defaulting via the
    # publisher lookup.
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="k",
    )
    assert config.venue_dataset_map == {"XNAS": "DBEQ.BASIC"}


def test_builder_uses_exchange_as_venue_default() -> None:
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="k",
    )
    assert config.use_exchange_as_venue is True


def test_builder_carries_api_key() -> None:
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
        api_key="dbn-secret-123",
    )
    assert config.api_key == "dbn-secret-123"


def test_builder_handles_multiple_native_venues() -> None:
    # Future-proofing the venue_dataset_map shape — even though PR 1 only
    # supports equities (XNAS), the builder must accept arbitrary keys.
    config = build_databento_data_client_config(
        native_instrument_ids=["AAPL.XNAS", "ESM6.GLBX"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC", "GLBX": "GLBX.MDP3"},
        api_key="k",
    )
    assert config.venue_dataset_map == {
        "XNAS": "DBEQ.BASIC",
        "GLBX": "GLBX.MDP3",
    }
    # Both instrument_ids parse correctly as InstrumentId objects
    assert {str(iid) for iid in (config.instrument_ids or [])} == {
        "AAPL.XNAS",
        "ESM6.GLBX",
    }


def test_resolved_targets_is_json_serializable_via_dict() -> None:
    # The targets cross the LiveCommandBus payload boundary as plain dicts.
    import json

    targets = ResolvedDatabentoTargets(
        native_instrument_ids=["AAPL.XNAS"],
        venue_dataset_map={"XNAS": "DBEQ.BASIC"},
    )
    payload = {
        "native_instrument_ids": targets.native_instrument_ids,
        "venue_dataset_map": targets.venue_dataset_map,
    }
    roundtrip = json.loads(json.dumps(payload))
    assert roundtrip == payload
