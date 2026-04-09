from __future__ import annotations

from typing import Any


def prepare_live_strategy_config(
    config: dict[str, Any],
    instruments: list[str],
) -> dict[str, Any]:
    if not instruments and "instrument_id" not in config:
        raise ValueError("At least one instrument is required")

    resolved = dict(config)
    instrument_id = str(resolved.get("instrument_id") or instruments[0]).strip()
    if not instrument_id:
        raise ValueError("instrument_id cannot be empty")
    if "." not in instrument_id:
        raise ValueError(
            "Live deployments require a venue-qualified Nautilus instrument ID such as AAPL.XNAS"
        )
    if instruments and instrument_id not in instruments:
        raise ValueError("config.instrument_id must match one of the selected instruments")

    resolved["instrument_id"] = instrument_id
    resolved["bar_type"] = _bar_type_for_instrument(
        instrument_id,
        resolved.get("bar_type"),
    )
    return resolved


def prepare_backtest_strategy_config(
    config: dict[str, Any],
    instruments: list[str],
) -> dict[str, Any]:
    if not instruments:
        raise ValueError("At least one instrument is required")

    resolved = dict(config)
    instrument_id = str(instruments[0]).strip()
    if not instrument_id:
        raise ValueError("instrument_id cannot be empty")

    resolved["instrument_id"] = instrument_id
    resolved["bar_type"] = _bar_type_for_instrument(
        instrument_id,
        resolved.get("bar_type"),
    )
    return resolved


def _bar_type_for_instrument(instrument_id: str, existing_bar_type: object | None) -> str:
    if existing_bar_type is not None:
        bar_type = str(existing_bar_type).strip()
        prefix, separator, suffix = bar_type.partition("-")
        if separator and prefix:
            return f"{instrument_id}-{suffix}"
    return f"{instrument_id}-1-MINUTE-LAST-EXTERNAL"
