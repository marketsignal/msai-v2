"""Shared JSON-Schema extraction for Nautilus ``StrategyConfig`` subclasses.

Used by ``strategy_registry`` discovery to populate ``config_schema`` +
``default_config`` on ``DiscoveredStrategy``. The resulting JSON Schema is
consumed by the frontend via ``GET /api/v1/strategies/{id}`` and drives the
auto-generated backtest-config form.

Why this module exists
----------------------
``msgspec.json.schema()`` raises ``TypeError`` on any custom class unless a
``schema_hook`` function is provided. Nautilus identifier classes
(``InstrumentId``, ``BarType``, ``Venue``, …) are custom Rust-backed
``frozen`` classes — they have no Python-level ``__annotations__`` for msgspec
to introspect. Without a hook, discovery would crash on every strategy.

This module installs a hook that maps all known Nautilus identifier classes
to JSON-Schema ``type: string`` with format hints the renderer can use to
pick a better widget later (``x-format: instrument-id`` → placeholder
``AAPL.NASDAQ``; ``x-format: bar-type`` → placeholder
``AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL``).

Verified behavior
-----------------
Pinned by the Phase 0 spike tests at
:mod:`tests.unit.test_strategy_registry.TestMsgspecSchemaFidelitySpike`
(2026-04-20 council pre-gate — 5/5 green):

* ``msgspec.json.schema(Cfg, schema_hook=nautilus_schema_hook)`` emits clean
  JSON Schema for the full ``EMACrossConfig`` layout including
  ``InstrumentId`` / ``BarType`` / ``Decimal`` / int / nullable fields.
* ``StrategyConfig.parse(json_string)`` is the authoritative round-trip —
  string-shaped payloads decode into typed instances and malformed values
  raise ``msgspec.ValidationError`` with ``$.<field>`` paths.
* ``Cfg.__annotations__.keys()`` lists only user-defined fields, so the
  schema returned by :func:`build_user_schema` trims inherited
  ``StrategyConfig`` base fields (``manage_stop``, ``order_id_tag``,
  ``external_order_claims``, …) that should never appear in the form.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import msgspec


class ConfigSchemaStatus(StrEnum):
    """Per-strategy extraction result.

    The API exposes this so the frontend can distinguish "no schema
    available" from "schema available and renderable" and from
    "extraction failed for this file". ``None`` on ``config_schema`` /
    ``default_config`` would conflate these three cases.
    """

    READY = "ready"
    UNSUPPORTED = "unsupported"
    EXTRACTION_FAILED = "extraction_failed"
    NO_CONFIG_CLASS = "no_config_class"


def nautilus_schema_hook(t: type) -> dict[str, Any]:
    """``schema_hook`` for ``msgspec.json.schema()`` covering Nautilus ID types.

    Imports are deferred into the function body so this module can be
    imported by tests or tooling that doesn't need the Nautilus runtime
    loaded.
    """
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

    if t is InstrumentId:
        return {
            "type": "string",
            "title": "Instrument ID",
            "x-format": "instrument-id",
            "description": "SYMBOL.VENUE",
            "examples": ["AAPL.NASDAQ", "EUR/USD.IDEALPRO"],
        }
    if t is BarType:
        return {
            "type": "string",
            "title": "Bar Type",
            "x-format": "bar-type",
            "description": "INSTRUMENT_ID-STEP-AGGREGATION-PRICE_TYPE-SOURCE",
            "examples": ["AAPL.NASDAQ-1-MINUTE-LAST-EXTERNAL"],
        }
    if t in (
        StrategyId,
        ComponentId,
        Venue,
        Symbol,
        AccountId,
        ClientId,
        OrderListId,
        PositionId,
        TraderId,
    ):
        return {"type": "string", "title": t.__name__}
    raise NotImplementedError(f"no schema hook for {t!r}")


def build_user_schema(
    config_cls: type | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, ConfigSchemaStatus]:
    """Extract (schema, defaults, status) for a ``StrategyConfig`` subclass.

    Returns ``(None, None, NO_CONFIG_CLASS)`` when ``config_cls`` is ``None``.
    On a Nautilus type the hook doesn't cover, returns
    ``(None, None, UNSUPPORTED)`` so discovery stays quiet and the operator
    gets a renderable status. Any other exception during extraction returns
    ``(None, None, EXTRACTION_FAILED)`` — the caller is responsible for
    logging the exception.

    On success, the returned ``schema`` dict is a standalone JSON-Schema
    object (``type: object``, ``properties: {...}``, ``required: [...]``)
    trimmed to the class's own ``__annotations__`` keys. Inherited
    ``StrategyConfig`` base fields are NOT included.

    The returned ``defaults`` dict maps field names to their msgspec-encoded
    default values (e.g. ``Decimal("1")`` → ``"1"``, nullable fields default
    to ``None``). Fields without defaults are omitted.
    """
    if config_cls is None:
        return (None, None, ConfigSchemaStatus.NO_CONFIG_CLASS)

    try:
        full = msgspec.json.schema(config_cls, schema_hook=nautilus_schema_hook)
    except NotImplementedError:
        # Hook rejected an unknown type — status signals "try adding coverage".
        return (None, None, ConfigSchemaStatus.UNSUPPORTED)
    except Exception:  # noqa: BLE001 — isolate per-strategy failures from discovery
        return (None, None, ConfigSchemaStatus.EXTRACTION_FAILED)

    own_keys = set(config_cls.__annotations__.keys())
    class_def = full.get("$defs", {}).get(config_cls.__name__)
    if class_def is None:
        # Defensive: msgspec's schema output layout changed. Treat as failure
        # rather than silently emitting a broken dict.
        return (None, None, ConfigSchemaStatus.EXTRACTION_FAILED)

    raw_props = class_def.get("properties", {})
    raw_required = class_def.get("required", [])

    trimmed_props = {k: v for k, v in raw_props.items() if k in own_keys}
    trimmed_required = [k for k in raw_required if k in own_keys]

    schema: dict[str, Any] = {
        "type": "object",
        "title": class_def.get("title") or config_cls.__name__,
        "properties": trimmed_props,
        "required": trimmed_required,
    }

    defaults = {k: v["default"] for k, v in trimmed_props.items() if "default" in v}

    return (schema, defaults, ConfigSchemaStatus.READY)
