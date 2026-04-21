"""Tests for server-authoritative ``asset_class`` derivation (Task B3).

Closes PR #39 scope-defer: the classifier / orchestrator must be able to
translate a symbol string (e.g. ``"ES.n.0"``) into the ingest-taxonomy
asset_class (``"futures"``) without relying on a caller hint.

Two public surfaces:

- ``derive_asset_class_sync(symbols)`` — shape-only, safe in any context.
- ``derive_asset_class(symbols, *, start, db)`` — async; prefers the
  instrument registry, falls back to the shape heuristic.

Plus: ``SecurityMaster.asset_class_for_alias(alias)`` — canonical alias
→ ingest-taxonomy name, or ``None`` on unknown shape.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from msai.services.backtests.derive_asset_class import (
    derive_asset_class,
    derive_asset_class_sync,
)


class TestDeriveAssetClassSync:
    """``derive_asset_class_sync`` — no DB, no async, shape-only."""

    @pytest.mark.parametrize(
        ("symbol", "expected"),
        [
            ("AAPL.NASDAQ", "stocks"),
            ("SPY.ARCA", "stocks"),
            ("MSFT.NYSE", "stocks"),
            ("AAPL.XNAS", "stocks"),
            ("ES.n.0", "futures"),
            ("ESM6.CME", "futures"),
            ("ESU24.GLBX", "futures"),
            ("EUR/USD.IDEALPRO", "forex"),
            ("SPY_CALL_400_20251231.OPRA", "options"),
        ],
    )
    def test_derive_from_shape(self, symbol: str, expected: str) -> None:
        assert derive_asset_class_sync([symbol]) == expected

    def test_unknown_symbol_returns_none(self) -> None:
        # iter-3 P2: shape-miss returns None so the classifier chain can
        # fall through to the caller-supplied ``asset_class`` hint and
        # the regex path-capture. A non-null default here silently
        # overrode correct hints (e.g. asset_class="options").
        assert derive_asset_class_sync(["Ω_WEIRD_SYMBOL"]) is None

    def test_empty_list_returns_none(self) -> None:
        # No symbols means no basis to infer; callers own the final default.
        assert derive_asset_class_sync([]) is None

    def test_mixed_asset_classes_returns_first_symbols_class(self) -> None:
        # Mixed-asset-class runs are rare and explicitly out of scope for
        # this heuristic — we return the first symbol's class rather than
        # raising, so the worker has *something* to echo in the remediation
        # command. The caller's asset_class hint takes over in practice.
        assert derive_asset_class_sync(["ES.n.0", "AAPL.NASDAQ"]) == "futures"
        assert derive_asset_class_sync(["AAPL.NASDAQ", "ES.n.0"]) == "stocks"


class TestDeriveAssetClassAsync:
    """``derive_asset_class`` — async; registry first, shape fallback."""

    async def test_registry_hit_wins_over_shape(self) -> None:
        # SecurityMaster resolves the symbol to a canonical alias; then
        # asset_class_for_alias returns the registry-authoritative answer.
        # The registry MUST win even if the shape would disagree.
        fake_master = MagicMock()
        fake_master.resolve_for_backtest = AsyncMock(return_value=["ESM6.CME"])
        fake_master.asset_class_for_alias = MagicMock(return_value="futures")

        fake_db = MagicMock()

        # Patch SecurityMaster constructor to return our mock.
        from msai.services.nautilus.security_master import service as sm_module

        original_ctor = sm_module.SecurityMaster
        sm_module.SecurityMaster = MagicMock(return_value=fake_master)  # type: ignore[misc]
        try:
            result = await derive_asset_class(["ES.n.0"], start=date(2024, 1, 1), db=fake_db)
        finally:
            sm_module.SecurityMaster = original_ctor  # type: ignore[misc]

        assert result == "futures"
        fake_master.resolve_for_backtest.assert_awaited_once()
        fake_master.asset_class_for_alias.assert_called_once_with("ESM6.CME")

    async def test_db_none_falls_back_to_shape(self) -> None:
        # No DB session → skip registry, use shape heuristic directly.
        result = await derive_asset_class(["ES.n.0"], start=date(2024, 1, 1), db=None)
        assert result == "futures"

    async def test_registry_exception_falls_back_to_shape(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A registry failure must never kill auto-heal. Log with exc_info
        # and fall back to the shape heuristic.
        fake_master = MagicMock()
        fake_master.resolve_for_backtest = AsyncMock(side_effect=RuntimeError("registry offline"))

        from msai.services.nautilus.security_master import service as sm_module

        original_ctor = sm_module.SecurityMaster
        sm_module.SecurityMaster = MagicMock(return_value=fake_master)  # type: ignore[misc]
        try:
            result = await derive_asset_class(["ES.n.0"], start=date(2024, 1, 1), db=MagicMock())
        finally:
            sm_module.SecurityMaster = original_ctor  # type: ignore[misc]

        assert result == "futures"  # shape fallback
        # structlog writes to stdout; verify warning + exc_info made it out.
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "asset_class_registry_lookup_failed" in combined
        assert "warning" in combined.lower()
        # exc_info=True should include the traceback.
        assert "RuntimeError" in combined
        assert "registry offline" in combined

    async def test_empty_list_returns_none_without_touching_registry(self) -> None:
        # Empty symbols → None; registry lookup is not attempted. Callers
        # own the final default via the ``or caller_hint or "stocks"`` chain
        # described in REV B7-v2.
        fake_master = MagicMock()
        fake_master.resolve_for_backtest = AsyncMock(return_value=["X"])

        from msai.services.nautilus.security_master import service as sm_module

        original_ctor = sm_module.SecurityMaster
        sm_module.SecurityMaster = MagicMock(return_value=fake_master)  # type: ignore[misc]
        try:
            result = await derive_asset_class([], start=date(2024, 1, 1), db=MagicMock())
        finally:
            sm_module.SecurityMaster = original_ctor  # type: ignore[misc]

        assert result is None
        fake_master.resolve_for_backtest.assert_not_called()


class TestAssetClassForAlias:
    """``SecurityMaster.asset_class_for_alias`` — registry → ingest taxonomy.

    Iter-2 P1-a: the public method MUST translate the registry's
    ``InstrumentSpec.asset_class`` values (``"equity"``, ``"future"``,
    ``"option"``, ``"forex"``) to the ingest / Parquet-storage taxonomy
    (``"stocks"``, ``"futures"``, ``"options"``, ``"forex"``).

    Without this mapping, writes land under ``data/parquet/equity/`` while
    the catalog reader expects ``data/parquet/stocks/`` — the auto-heal
    coverage re-check then fails forever.
    """

    @pytest.mark.parametrize(
        ("alias", "expected"),
        [
            ("AAPL.NASDAQ", "stocks"),  # equity → stocks
            ("SPY.ARCA", "stocks"),
            ("ESM6.CME", "futures"),  # future → futures
            ("EUR/USD.IDEALPRO", "forex"),  # forex stays forex
        ],
    )
    def test_registry_taxonomy_translates_to_ingest_taxonomy(
        self, alias: str, expected: str
    ) -> None:
        from msai.services.nautilus.security_master.service import SecurityMaster

        # Build a SecurityMaster with mocked deps; we only exercise the
        # pure-function path through ``_spec_from_canonical``.
        master = SecurityMaster.__new__(SecurityMaster)
        assert master.asset_class_for_alias(alias) == expected

    def test_unknown_venue_returns_none(self) -> None:
        from msai.services.nautilus.security_master.service import SecurityMaster

        master = SecurityMaster.__new__(SecurityMaster)
        assert master.asset_class_for_alias("FOO.UNKNOWN_VENUE") is None

    def test_empty_alias_returns_none(self) -> None:
        from msai.services.nautilus.security_master.service import SecurityMaster

        master = SecurityMaster.__new__(SecurityMaster)
        assert master.asset_class_for_alias("") is None
