from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import structlog
import yaml
from dateutil.relativedelta import relativedelta

from msai.schemas.symbol_onboarding import AssetClass, OnboardSymbolSpec

log = structlog.get_logger(__name__)

__all__ = [
    "ManifestParseError",
    "ParsedManifest",
    "parse_manifest_file",
    "merge_manifests",
]

_WATCHLIST_NAME_RE = re.compile(r"^[a-z0-9\-]+$")
_ALLOWED_SYMBOL_KEYS = frozenset({"symbol", "asset_class", "start", "end", "window"})
_ALLOWED_TOP_KEYS = frozenset({"watchlist_name", "symbols"})
_TRAILING_5Y_WINDOW = "trailing_5y"


class ManifestParseError(ValueError):
    """Raised when a watchlist YAML is syntactically or semantically invalid."""


@dataclass(frozen=True, slots=True)
class ParsedManifest:
    watchlist_name: str
    symbols: list[OnboardSymbolSpec]


def parse_manifest_file(path: Path, *, today: date | None = None) -> ParsedManifest:
    if not path.is_file():
        raise ManifestParseError(f"Manifest file not found: {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ManifestParseError("Manifest root must be a mapping")

    unknown_top = set(raw.keys()) - _ALLOWED_TOP_KEYS
    if unknown_top:
        raise ManifestParseError(f"Unknown top-level keys: {sorted(unknown_top)}")

    name = raw.get("watchlist_name")
    if not isinstance(name, str) or not _WATCHLIST_NAME_RE.match(name):
        raise ManifestParseError(
            f"watchlist_name must match {_WATCHLIST_NAME_RE.pattern}; got {name!r}"
        )

    symbols_raw = raw.get("symbols")
    if not isinstance(symbols_raw, list) or not symbols_raw:
        raise ManifestParseError("symbols: must be a non-empty list")

    resolved = [_parse_symbol_entry(entry, today=today or date.today()) for entry in symbols_raw]
    return ParsedManifest(watchlist_name=name, symbols=resolved)


def _parse_symbol_entry(entry: Any, *, today: date) -> OnboardSymbolSpec:
    if not isinstance(entry, dict):
        raise ManifestParseError(f"symbols[*] must be a mapping; got {type(entry).__name__}")

    unknown = set(entry.keys()) - _ALLOWED_SYMBOL_KEYS
    if unknown:
        raise ManifestParseError(f"Unknown symbol-entry keys: {sorted(unknown)}")

    window_sugar = entry.get("window")
    if window_sugar is not None and ("start" in entry or "end" in entry):
        raise ManifestParseError("window: sugar cannot be combined with explicit start/end")

    if window_sugar is not None:
        if window_sugar != _TRAILING_5Y_WINDOW:
            raise ManifestParseError(f"Unsupported window sugar: {window_sugar!r}")
        end = today - relativedelta(days=1)
        start = end - relativedelta(years=5)
    else:
        start = _coerce_date(entry.get("start"), "start")
        end = _coerce_date(entry.get("end"), "end")

    asset_class_str = str(entry["asset_class"]).strip()
    return OnboardSymbolSpec(
        symbol=str(entry["symbol"]).strip(),
        asset_class=cast("AssetClass", asset_class_str),
        start=start,
        end=end,
    )


def _coerce_date(value: Any, field: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ManifestParseError(f"{field}: not ISO 8601 date: {value!r}") from exc
    raise ManifestParseError(f"{field}: required (date or YYYY-MM-DD)")


def merge_manifests(manifests: list[ParsedManifest], *, merged_name: str) -> ParsedManifest:
    """Combine multiple manifests into one; wider window wins on duplicate keys."""

    pool: dict[tuple[str, str], OnboardSymbolSpec] = {}
    for m in manifests:
        for spec in m.symbols:
            key = (spec.symbol, spec.asset_class)
            existing = pool.get(key)
            if existing is None:
                pool[key] = spec
                continue
            widened_start = min(existing.start, spec.start)
            widened_end = max(existing.end, spec.end)
            if (widened_start, widened_end) != (existing.start, existing.end):
                log.info(
                    "manifest_dedup_widened",
                    symbol=spec.symbol,
                    asset_class=spec.asset_class,
                    prior_window=[existing.start.isoformat(), existing.end.isoformat()],
                    merged_window=[widened_start.isoformat(), widened_end.isoformat()],
                )
            pool[key] = OnboardSymbolSpec(
                symbol=spec.symbol,
                asset_class=spec.asset_class,
                start=widened_start,
                end=widened_end,
            )
    return ParsedManifest(
        watchlist_name=merged_name,
        symbols=sorted(
            pool.values(),
            key=lambda s: (s.asset_class, s.symbol),
        ),
    )
