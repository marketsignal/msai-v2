"""Unit tests for msai.services.nautilus.schema_hooks.

Companion to the Phase 0 spike at
:mod:`tests.unit.test_strategy_registry.TestMsgspecSchemaFidelitySpike` —
the spike proved msgspec behavior; these tests pin the shipping API
(``nautilus_schema_hook`` + ``build_user_schema`` + ``ConfigSchemaStatus``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ComponentId,
    InstrumentId,
    OrderListId,
    PositionId,
    StrategyId,
    Symbol,
    TraderId,
    Venue,
)
from nautilus_trader.trading.config import StrategyConfig

from msai.services.nautilus.schema_hooks import (
    ConfigSchemaStatus,
    build_user_schema,
    nautilus_schema_hook,
)


# Module-scope declaration so `from __future__ import annotations` forward
# refs resolve against real class objects when msgspec introspects.
class _TestEMACrossConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    fast_ema_period: int = 10
    slow_ema_period: int = 30
    trade_size: Decimal = Decimal("1")


class _TestEmptyConfig(StrategyConfig, frozen=True):
    """Config with no user-defined fields, to exercise the trim path."""


# ---------------------------------------------------------------------------
# nautilus_schema_hook — covers all known Nautilus ID types
# ---------------------------------------------------------------------------


class TestNautilusSchemaHook:
    def test_instrument_id_has_format_hint_and_examples(self) -> None:
        out = nautilus_schema_hook(InstrumentId)
        assert out["type"] == "string"
        assert out["x-format"] == "instrument-id"
        assert "AAPL.NASDAQ" in out["examples"]

    def test_bar_type_has_format_hint_and_examples(self) -> None:
        out = nautilus_schema_hook(BarType)
        assert out["type"] == "string"
        assert out["x-format"] == "bar-type"
        assert any("MINUTE" in ex for ex in out["examples"])

    @pytest.mark.parametrize(
        "t",
        [
            StrategyId,
            ComponentId,
            Venue,
            Symbol,
            AccountId,
            ClientId,
            OrderListId,
            PositionId,
            TraderId,
        ],
    )
    def test_plain_nautilus_id_types_are_typed_strings(self, t: type) -> None:
        out = nautilus_schema_hook(t)
        assert out == {"type": "string", "title": t.__name__}

    def test_unknown_type_raises_not_implemented(self) -> None:
        class Foreign:
            pass

        with pytest.raises(NotImplementedError, match="Foreign"):
            nautilus_schema_hook(Foreign)


# ---------------------------------------------------------------------------
# ConfigSchemaStatus
# ---------------------------------------------------------------------------


class TestConfigSchemaStatus:
    def test_has_four_stable_values(self) -> None:
        assert {s.value for s in ConfigSchemaStatus} == {
            "ready",
            "unsupported",
            "extraction_failed",
            "no_config_class",
        }


# ---------------------------------------------------------------------------
# build_user_schema — the full happy path + every degraded branch
# ---------------------------------------------------------------------------


class TestBuildUserSchema:
    def test_happy_path_returns_schema_defaults_ready(self) -> None:
        schema, defaults, status = build_user_schema(_TestEMACrossConfig)

        assert status is ConfigSchemaStatus.READY
        assert schema is not None
        assert defaults is not None

        # User fields only — inherited StrategyConfig plumbing is trimmed.
        props = schema["properties"]
        assert set(props.keys()) == {
            "instrument_id",
            "bar_type",
            "fast_ema_period",
            "slow_ema_period",
            "trade_size",
        }

        # Primitive types encoded correctly
        assert props["fast_ema_period"] == {"type": "integer", "default": 10}
        assert props["trade_size"]["type"] == "string"
        assert props["trade_size"]["format"] == "decimal"

        # Nautilus types carry format hints from the schema_hook
        assert props["instrument_id"]["x-format"] == "instrument-id"
        assert props["bar_type"]["x-format"] == "bar-type"

        # Defaults dict mirrors what the form should pre-fill
        assert defaults["fast_ema_period"] == 10
        assert defaults["slow_ema_period"] == 30
        assert defaults["trade_size"] == "1"
        # Required (no-default) fields omitted from defaults
        assert "instrument_id" not in defaults
        assert "bar_type" not in defaults

    def test_required_list_excludes_inherited_fields(self) -> None:
        schema, _, _ = build_user_schema(_TestEMACrossConfig)
        assert schema is not None
        assert set(schema["required"]) == {"instrument_id", "bar_type"}

    def test_no_config_class_returns_none_status(self) -> None:
        schema, defaults, status = build_user_schema(None)
        assert schema is None
        assert defaults is None
        assert status is ConfigSchemaStatus.NO_CONFIG_CLASS

    def test_empty_config_still_ready_with_no_user_fields(self) -> None:
        # Empty user field set — schema is valid but properties is {}
        schema, defaults, status = build_user_schema(_TestEmptyConfig)
        assert status is ConfigSchemaStatus.READY
        assert schema is not None
        assert schema["properties"] == {}
        assert defaults == {}

    def test_unsupported_type_returns_unsupported_status(self) -> None:
        """A config that references a type the hook doesn't cover must
        degrade to UNSUPPORTED — not poison discovery."""

        class UnknownType:
            pass

        class ExoticConfig(StrategyConfig, frozen=True):
            # msgspec will try to schema-encode UnknownType → hook raises
            # NotImplementedError → build_user_schema returns UNSUPPORTED.
            exotic: Any = None

        # Force msgspec to see UnknownType by constructing a config that
        # msgspec cannot encode. The simplest robust way: hand the function
        # a class whose ``__annotations__`` advertises the UnknownType.
        ExoticConfig.__annotations__ = {"exotic": UnknownType}

        schema, defaults, status = build_user_schema(ExoticConfig)
        # Extraction either fails with UNSUPPORTED (via NotImplementedError
        # from the hook) OR EXTRACTION_FAILED (if msgspec raises something
        # else when it can't introspect UnknownType). Both are acceptable —
        # the invariant is that the status is NOT READY.
        assert status is not ConfigSchemaStatus.READY
        assert schema is None
        assert defaults is None
