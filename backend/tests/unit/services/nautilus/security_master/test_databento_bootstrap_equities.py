"""Unit tests for :class:`DatabentoBootstrapService` on equity symbols.

Exercises the session-per-symbol orchestration, dataset-fallback ordering,
ambiguity-per-symbol propagation, semaphore concurrency cap, and
``exact_id`` pass-through (post-normalization canonical id).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from msai.services.nautilus.security_master.databento_bootstrap import (
    BootstrapOutcome,
    DatabentoBootstrapService,
)

pytest_plugins = ["tests.integration.conftest_databento"]


@pytest.mark.asyncio
async def test_bootstrap_aapl_returns_created(session_factory, mock_databento):
    svc = DatabentoBootstrapService(
        session_factory=session_factory,
        databento_client=mock_databento,
        max_concurrent=3,
    )
    results = await svc.bootstrap(symbols=["AAPL"], asset_class_override=None, exact_ids=None)
    assert len(results) == 1
    r = results[0]
    assert r.symbol == "AAPL"
    assert r.outcome == BootstrapOutcome.CREATED
    assert r.registered is True
    assert r.live_qualified is False
    assert r.canonical_id == "AAPL.NASDAQ"
    assert r.asset_class == "equity"


@pytest.mark.asyncio
async def test_bootstrap_ambiguous_per_symbol(session_factory, mock_databento):
    svc = DatabentoBootstrapService(
        session_factory=session_factory,
        databento_client=mock_databento,
    )
    results = await svc.bootstrap(
        symbols=["AAPL", "BRK.B"], asset_class_override=None, exact_ids=None
    )
    outcomes = {r.symbol: r.outcome for r in results}
    assert outcomes["AAPL"] == BootstrapOutcome.CREATED
    assert outcomes["BRK.B"] == BootstrapOutcome.AMBIGUOUS
    brk_b = next(r for r in results if r.symbol == "BRK.B")
    assert len(brk_b.candidates) >= 2


@pytest.mark.asyncio
async def test_bootstrap_dataset_fallback(session_factory, mock_databento):
    """First dataset 401s; second succeeds. Asserts ordered fallback AND
    result.dataset reflects the dataset that succeeded."""
    from msai.services.data_sources.databento_errors import DatabentoUnauthorizedError
    from tests.integration.conftest_databento import _make_equity_instrument

    call_log = []

    async def _side_effect(symbol, start, end, *, dataset, target_path, exact_id=None):
        call_log.append(dataset)
        if dataset == "XNAS.ITCH":
            raise DatabentoUnauthorizedError("401", http_status=401, dataset=dataset)
        return [_make_equity_instrument("UNKN", "XARC")]

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_side_effect)
    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )
    results = await svc.bootstrap(symbols=["UNKN"], asset_class_override=None, exact_ids=None)

    assert call_log == ["XNAS.ITCH", "XNYS.PILLAR"]
    assert results[0].outcome == BootstrapOutcome.CREATED
    assert results[0].dataset == "XNYS.PILLAR"
    assert results[0].registered is True


@pytest.mark.asyncio
async def test_max_concurrent_3_cap_honored(session_factory, mock_databento):
    import asyncio

    in_flight = {"max": 0, "current": 0}
    original_side_effect = mock_databento.fetch_definition_instruments.side_effect

    async def _tracked(symbol, start, end, *, dataset, target_path, exact_id=None):
        in_flight["current"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["current"])
        await asyncio.sleep(0.05)
        try:
            return original_side_effect(
                symbol, start, end, dataset=dataset, target_path=target_path, exact_id=exact_id
            )
        finally:
            in_flight["current"] -= 1

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_tracked)
    svc = DatabentoBootstrapService(
        session_factory=session_factory,
        databento_client=mock_databento,
        max_concurrent=3,
    )
    await svc.bootstrap(
        symbols=["AAPL", "SPY", "QQQ", "AAPL", "SPY"],
        asset_class_override=None,
        exact_ids=None,
    )
    assert in_flight["max"] <= 3


@pytest.mark.asyncio
async def test_bootstrap_exact_id_resolves_single(session_factory, mock_databento):
    """With exact_ids, the service passes exact_id down to
    fetch_definition_instruments which pre-filters before ambiguity raise.

    Also asserts the `exact_id` kwarg is forwarded verbatim (guards the
    dispatch path against a regression that drops it)."""
    from tests.integration.conftest_databento import _make_equity_instrument

    mock_databento.fetch_definition_instruments = AsyncMock(
        return_value=[_make_equity_instrument("BRK.B", "XNYS")]
    )

    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )
    results = await svc.bootstrap(
        symbols=["BRK.B"],
        asset_class_override=None,
        exact_ids={"BRK.B": "BRK.B.XNYS"},
    )
    assert results[0].outcome == BootstrapOutcome.CREATED
    assert results[0].registered is True
    # canonical_id is POST-normalization (XNYS → NYSE per venue map).
    assert results[0].canonical_id == "BRK.B.NYSE"
    # exact_id forwarded through to the client
    call_kwargs = mock_databento.fetch_definition_instruments.call_args.kwargs
    assert call_kwargs["exact_id"] == "BRK.B.XNYS"


@pytest.mark.asyncio
async def test_bootstrap_unmapped_venue_outcome(session_factory, mock_databento):
    """Databento returns an instrument whose venue suffix is NOT in the
    closed MIC→exchange-name map. Service catches UnknownDatabentoVenueError
    and returns outcome=UNMAPPED_VENUE (with rollback)."""
    from tests.integration.conftest_databento import _make_equity_instrument

    mock_databento.fetch_definition_instruments = AsyncMock(
        return_value=[_make_equity_instrument("FOO", "NOSUCHMIC")]
    )

    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )
    results = await svc.bootstrap(symbols=["FOO"], asset_class_override=None, exact_ids=None)
    assert results[0].outcome == BootstrapOutcome.UNMAPPED_VENUE
    assert results[0].registered is False
    assert results[0].canonical_id is None
    assert results[0].diagnostics is not None
    assert "NOSUCHMIC" in results[0].diagnostics


@pytest.mark.asyncio
async def test_bootstrap_upstream_error_all_datasets(session_factory, mock_databento):
    """All 3 equity datasets raise DatabentoUpstreamError; the final result
    surfaces outcome=UPSTREAM_ERROR with diagnostics from the highest-severity
    failure (tie-break keeps the first one)."""
    from msai.services.data_sources.databento_errors import DatabentoUpstreamError

    async def _side_effect(symbol, start, end, *, dataset, target_path, exact_id=None):
        raise DatabentoUpstreamError(f"upstream 502 on {dataset}", http_status=502, dataset=dataset)

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_side_effect)
    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )
    results = await svc.bootstrap(symbols=["NOPE"], asset_class_override=None, exact_ids=None)
    assert results[0].outcome == BootstrapOutcome.UPSTREAM_ERROR
    assert results[0].registered is False
    assert results[0].diagnostics is not None


@pytest.mark.asyncio
async def test_bootstrap_rate_limited_does_not_fallback(session_factory, mock_databento):
    """First dataset returns 429; service short-circuits (does NOT try
    next dataset — retrying would re-trip the same quota)."""
    from msai.services.data_sources.databento_errors import DatabentoRateLimitedError

    call_log = []

    async def _side_effect(symbol, start, end, *, dataset, target_path, exact_id=None):
        call_log.append(dataset)
        raise DatabentoRateLimitedError(f"429 on {dataset}", http_status=429, dataset=dataset)

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_side_effect)
    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )
    results = await svc.bootstrap(symbols=["NOPE"], asset_class_override=None, exact_ids=None)
    # Only ONE dataset attempted — rate-limit short-circuits.
    assert call_log == ["XNAS.ITCH"]
    assert results[0].outcome == BootstrapOutcome.RATE_LIMITED
    assert results[0].registered is False


@pytest.mark.asyncio
async def test_bootstrap_severity_ranking_keeps_unauthorized_over_upstream(
    session_factory, mock_databento
):
    """Dataset 1 returns 401, dataset 2 returns 500. Final result surfaces
    UNAUTHORIZED (more actionable for operator) — NOT the later 500."""
    from msai.services.data_sources.databento_errors import (
        DatabentoUnauthorizedError,
        DatabentoUpstreamError,
    )

    async def _side_effect(symbol, start, end, *, dataset, target_path, exact_id=None):
        if dataset == "XNAS.ITCH":
            raise DatabentoUnauthorizedError("401", http_status=401, dataset=dataset)
        raise DatabentoUpstreamError(f"500 on {dataset}", http_status=500, dataset=dataset)

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_side_effect)
    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )
    results = await svc.bootstrap(symbols=["NOPE"], asset_class_override=None, exact_ids=None)
    assert results[0].outcome == BootstrapOutcome.UNAUTHORIZED


@pytest.mark.asyncio
async def test_bootstrap_gather_preserves_partial_progress(session_factory, mock_databento):
    """If one symbol raises an unexpected exception, other symbols in the
    batch still produce results — the failing symbol materializes as a
    synthetic UPSTREAM_ERROR row so callers see partial progress."""
    from tests.integration.conftest_databento import _make_equity_instrument

    def _side_effect(symbol, start, end, *, dataset, target_path, exact_id=None):
        if symbol == "CRASH":
            raise ValueError("unexpected shape from SDK")
        if symbol == "AAPL":
            return [_make_equity_instrument("AAPL", "XNAS")]
        if symbol == "SPY":
            return [_make_equity_instrument("SPY", "XARC")]
        raise AssertionError(f"unexpected symbol {symbol}")

    mock_databento.fetch_definition_instruments = AsyncMock(side_effect=_side_effect)

    svc = DatabentoBootstrapService(
        session_factory=session_factory, databento_client=mock_databento
    )
    results = await svc.bootstrap(
        symbols=["AAPL", "CRASH", "SPY"], asset_class_override=None, exact_ids=None
    )
    by_sym = {r.symbol: r for r in results}
    assert by_sym["AAPL"].outcome == BootstrapOutcome.CREATED
    assert by_sym["CRASH"].outcome == BootstrapOutcome.UPSTREAM_ERROR
    assert by_sym["CRASH"].registered is False
    assert "ValueError" in (by_sym["CRASH"].diagnostics or "")
    assert by_sym["SPY"].outcome == BootstrapOutcome.CREATED
