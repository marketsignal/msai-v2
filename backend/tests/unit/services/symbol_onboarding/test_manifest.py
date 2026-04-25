from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest

from msai.schemas.symbol_onboarding import OnboardSymbolSpec
from msai.services.symbol_onboarding.manifest import (
    ManifestParseError,
    ParsedManifest,
    merge_manifests,
    parse_manifest_file,
)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(body))
    return p


def test_parse_manifest_explicit_window(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "core.yaml",
        """
        watchlist_name: core-equities
        symbols:
          - symbol: SPY
            asset_class: equity
            start: 2023-01-01
            end: 2024-12-31
        """,
    )
    result = parse_manifest_file(f)
    assert isinstance(result, ParsedManifest)
    assert result.watchlist_name == "core-equities"
    assert len(result.symbols) == 1
    spec = result.symbols[0]
    assert spec.symbol == "SPY"
    assert spec.start == date(2023, 1, 1)
    assert spec.end == date(2024, 12, 31)


def test_parse_manifest_trailing_5y_sugar_uses_yesterday(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "trailing.yaml",
        """
        watchlist_name: rolling
        symbols:
          - symbol: AAPL
            asset_class: equity
            window: trailing_5y
        """,
    )
    result = parse_manifest_file(f, today=date(2026, 4, 24))
    spec = result.symbols[0]
    assert spec.end == date(2026, 4, 23)
    assert spec.start == date(2021, 4, 23)


def test_parse_manifest_rejects_unknown_key(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "bad.yaml",
        """
        watchlist_name: x
        symbols:
          - symbol: SPY
            asset_class: equity
            start: 2024-01-01
            end: 2024-12-31
            bogus_field: 1
        """,
    )
    with pytest.raises(ManifestParseError, match="bogus_field"):
        parse_manifest_file(f)


def test_parse_manifest_rejects_trailing_5y_with_explicit_window(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "conflict.yaml",
        """
        watchlist_name: x
        symbols:
          - symbol: SPY
            asset_class: equity
            window: trailing_5y
            start: 2024-01-01
            end: 2024-12-31
        """,
    )
    with pytest.raises(ManifestParseError, match="window.*cannot.*start.*end"):
        parse_manifest_file(f)


def test_parse_manifest_watchlist_name_slug_rule(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        "bad_name.yaml",
        """
        watchlist_name: Core Equities!
        symbols:
          - symbol: SPY
            asset_class: equity
            start: 2024-01-01
            end: 2024-12-31
        """,
    )
    with pytest.raises(ManifestParseError, match="watchlist_name"):
        parse_manifest_file(f)


def test_merge_manifests_widens_window_for_duplicate_symbol() -> None:
    m1 = ParsedManifest(
        watchlist_name="a",
        symbols=[_spec("SPY", "equity", date(2024, 1, 1), date(2024, 6, 30))],
    )
    m2 = ParsedManifest(
        watchlist_name="b",
        symbols=[_spec("SPY", "equity", date(2023, 1, 1), date(2024, 12, 31))],
    )
    merged = merge_manifests([m1, m2], merged_name="combined")
    assert len(merged.symbols) == 1
    assert merged.symbols[0].start == date(2023, 1, 1)
    assert merged.symbols[0].end == date(2024, 12, 31)


def test_merge_manifests_keeps_distinct_asset_classes_separate() -> None:
    m = ParsedManifest(
        watchlist_name="m",
        symbols=[
            _spec("ES", "equity", date(2024, 1, 1), date(2024, 12, 31)),
            _spec("ES", "futures", date(2024, 1, 1), date(2024, 12, 31)),
        ],
    )
    merged = merge_manifests([m], merged_name="m")
    assert len(merged.symbols) == 2


def _spec(symbol: str, asset_class: str, start: date, end: date) -> OnboardSymbolSpec:
    return OnboardSymbolSpec(symbol=symbol, asset_class=asset_class, start=start, end=end)
