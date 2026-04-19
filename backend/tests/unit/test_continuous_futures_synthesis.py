from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from msai.services.nautilus.security_master.continuous_futures import (
    ResolvedInstrumentDefinition,
    continuous_needs_refresh_for_window,
    definition_window_bounds_from_details,
    resolved_databento_definition,
)

if TYPE_CHECKING:
    import pytest


def _mock_futures_instrument(
    raw_symbol: str, venue: str, activation_ns: int, expiration_ns: int
) -> MagicMock:
    """Build a mock Nautilus FuturesContract stand-in. Full Instrument
    instantiation requires all ~15 mandatory fields; a MagicMock is fine
    for testing the synthesis logic."""
    inst = MagicMock()
    inst.raw_symbol.value = raw_symbol
    inst.id.venue.value = venue
    return inst


def test_resolved_databento_definition_synthesizes_continuous_on_cme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange — a single June-2026 ES contract loaded from Databento with
    # use_exchange_as_venue=True so the venue is "CME".
    mock_inst = _mock_futures_instrument("ESM6", "CME", 1000, 9000)
    mock_inst.id.__str__ = MagicMock(return_value="ESM6.CME")
    payload_returned = {
        "type": "FuturesContract",
        "id": "ESM6.CME",
        "raw_symbol": "ESM6",
        "ts_init": 12345,
        "activation_ns": 1000,
        "expiration_ns": 9000,
    }
    # Patch instrument_to_payload as imported into the module under test
    monkeypatch.setattr(
        "msai.services.nautilus.security_master.continuous_futures.instrument_to_payload",
        lambda _: dict(payload_returned),
    )

    resolved = resolved_databento_definition(
        raw_symbol="ES.Z.5",
        instruments=[mock_inst],
        dataset="GLBX.MDP3",
        start="2024-01-01",
        end="2024-12-31",
        definition_path="/tmp/fake.definition.dbn.zst",
    )
    # Synthetic ID preserves .Z.N + the underlying venue
    assert isinstance(resolved, ResolvedInstrumentDefinition)
    assert resolved.instrument_id == "ES.Z.5.CME"
    assert resolved.raw_symbol == "ES.Z.5"
    assert resolved.listing_venue == "CME"
    assert resolved.routing_venue == "CME"
    assert resolved.provider == "databento"
    assert resolved.contract_details["requested_symbol"] == "ES.Z.5"


def test_definition_window_bounds_extracts_from_contract_details() -> None:
    bounds = definition_window_bounds_from_details({
        "definition_start": "2024-01-01",
        "definition_end": "2024-12-31",
    })
    assert bounds == ("2024-01-01", "2024-12-31")


def test_continuous_needs_refresh_when_window_expands() -> None:
    # Cached window [2024-01-01, 2024-12-31]; request 2024-01-01..2025-06-30
    needs = continuous_needs_refresh_for_window(
        cached_start="2024-01-01",
        cached_end="2024-12-31",
        requested_start="2024-01-01",
        requested_end="2025-06-30",
    )
    assert needs is True


def test_continuous_no_refresh_when_window_covered() -> None:
    needs = continuous_needs_refresh_for_window(
        cached_start="2024-01-01",
        cached_end="2024-12-31",
        requested_start="2024-03-01",
        requested_end="2024-06-30",
    )
    assert needs is False
