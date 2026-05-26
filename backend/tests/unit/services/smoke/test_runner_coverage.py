"""Unit tests for the coverage-check + full-range mutex helpers on
:mod:`msai.services.smoke.runner` (code-review iter-1 fix #4).

Two behaviours under test:

1. ``_iter_month_starts`` enumerates one first-of-month ``date`` per month
   in ``[start, end]`` so :func:`_ensure_ingested` can acquire a lock per
   ``(symbol, YYYY-MM)``. Pre-fix, ``_ensure_ingested`` locked only the
   start month; a concurrent run hitting December got no protection.

2. ``_ensure_ingested`` short-circuits when ``_symbols_with_gaps`` returns
   an empty list (warm-path: parquet already covers the full window). In
   the warm path neither the lock backend nor ``ingest_symbols`` is
   touched, satisfying the warm-path runtime budget. In the cold path
   ``ingest_symbols`` is invoked once with the gapped subset, and every
   ``(symbol, month)`` slot is mutex-acquired before the fetch.

Imports are intentionally co-located with their uses to defeat the
PostToolUse ruff formatter's "unused import" strip (see
``feedback_colocate_imports_with_usage_in_edits.md``).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from msai.services.smoke.runner import (
    _ensure_ingested,
    _iter_month_starts,
)

# ---------------------------------------------------------------------------
# _iter_month_starts
# ---------------------------------------------------------------------------


def test_iter_month_starts_single_month_returns_one_value() -> None:
    """A start..end window inside one month yields one first-of-month date."""
    months = _iter_month_starts(date(2024, 12, 1), date(2024, 12, 31))
    assert months == [date(2024, 12, 1)]


def test_iter_month_starts_full_year_returns_twelve_values_in_order() -> None:
    """The smoke:nightly window (Jan 1 → Dec 31) covers all 12 months."""
    months = _iter_month_starts(date(2024, 1, 1), date(2024, 12, 31))
    assert months == [date(2024, m, 1) for m in range(1, 13)]


def test_iter_month_starts_crosses_year_boundary() -> None:
    """A multi-year window collects months from both years in order."""
    months = _iter_month_starts(date(2023, 11, 15), date(2024, 2, 5))
    assert months == [
        date(2023, 11, 1),
        date(2023, 12, 1),
        date(2024, 1, 1),
        date(2024, 2, 1),
    ]


def test_iter_month_starts_inverted_window_returns_empty() -> None:
    """end < start is a degenerate window — return nothing rather than raise."""
    assert _iter_month_starts(date(2024, 12, 1), date(2024, 1, 1)) == []


# ---------------------------------------------------------------------------
# _ensure_ingested warm-path: coverage="full" → skip ingest entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_ingested_skips_lock_and_fetch_when_coverage_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every symbol is already fully covered, no Redis call + no fetch.

    Code-review iter-1 fix #4: the previous runner always called
    ``ingest_symbols`` even when parquet was already on disk for the full
    window, which violated the warm-path runtime budget.
    """
    calls: dict[str, int] = {"get_redis_pool": 0, "ingest_symbols": 0}

    async def _stub_get_pool() -> object:
        calls["get_redis_pool"] += 1
        return object()

    async def _stub_ingest_symbols(*_args: Any, **_kwargs: Any) -> object:
        calls["ingest_symbols"] += 1
        return object()

    async def _stub_symbols_with_gaps(
        _symbols: tuple[str, ...],
        _start: date,
        _end: date,
    ) -> list[str]:
        # Warm path: nothing missing.
        return []

    monkeypatch.setattr("msai.services.smoke.runner.get_redis_pool", _stub_get_pool)
    monkeypatch.setattr("msai.services.smoke.runner.ingest_symbols", _stub_ingest_symbols)
    monkeypatch.setattr("msai.services.smoke.runner._symbols_with_gaps", _stub_symbols_with_gaps)

    # Act
    await _ensure_ingested(("AAPL", "SPY"), "2024-12-01", "2024-12-31")

    # Assert: warm-path short-circuit fired before any external call.
    assert calls["get_redis_pool"] == 0, (
        "Warm path must not touch Redis when coverage is already full"
    )
    assert calls["ingest_symbols"] == 0, (
        "Warm path must not call ingest_symbols when coverage is already full"
    )


# ---------------------------------------------------------------------------
# _ensure_ingested cold-path: acquires one lock per (symbol, month)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_ingested_acquires_lock_per_symbol_and_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutex now covers every month in [start, end] — not just the first.

    Previously ``_ensure_ingested`` locked ``window_start`` only, so a
    concurrent ``smoke:nightly`` invocation hitting December got no
    protection while the original ran January. The fix iterates
    ``_iter_month_starts`` and acquires one lock per ``(symbol, YYYY-MM)``.
    """
    acquired: list[tuple[str, date]] = []
    released: list[tuple[str, date]] = []
    ingest_calls: list[list[str]] = []

    async def _stub_get_pool() -> object:
        return object()

    async def _stub_acquire(
        _pool: object,
        *,
        symbol: str,
        window_start: date,
        ttl_seconds: int,
        wait_timeout_seconds: int,
    ) -> str:
        # Cold-nightly budget guard: TTL must comfortably exceed the
        # default 900s previously hard-coded; the runner bumps to 3600s
        # to accommodate a 12-month cold pull. Assert here so any future
        # regression to a too-short TTL fails this unit test.
        assert ttl_seconds >= 3600, (
            f"ttl_seconds={ttl_seconds!r} is too short for cold-nightly ingest"
        )
        acquired.append((symbol, window_start))
        return f"token-{symbol}-{window_start.isoformat()}"

    async def _stub_release(
        _pool: object,
        *,
        symbol: str,
        window_start: date,
        token: str,
    ) -> None:
        released.append((symbol, window_start))

    async def _stub_ingest_symbols(
        _asset_class: str,
        symbols: list[str],
        _start: str,
        _end: str,
        **_kwargs: Any,
    ) -> object:
        ingest_calls.append(list(symbols))
        return object()

    async def _stub_symbols_with_gaps(
        symbols: tuple[str, ...],
        _start: date,
        _end: date,
    ) -> list[str]:
        return list(symbols)

    monkeypatch.setattr("msai.services.smoke.runner.get_redis_pool", _stub_get_pool)
    monkeypatch.setattr("msai.services.smoke.runner.acquire_ingest_lock", _stub_acquire)
    monkeypatch.setattr("msai.services.smoke.runner.release_ingest_lock", _stub_release)
    monkeypatch.setattr("msai.services.smoke.runner.ingest_symbols", _stub_ingest_symbols)
    monkeypatch.setattr("msai.services.smoke.runner._symbols_with_gaps", _stub_symbols_with_gaps)

    # Act: nightly window = Jan 1 → Dec 31 (12 months × 2 symbols = 24 locks).
    await _ensure_ingested(("AAPL", "SPY"), "2024-01-01", "2024-12-31")

    # Assert: 24 locks acquired and 24 released, covering every month.
    expected = {
        (symbol, date(2024, month, 1)) for symbol in ("AAPL", "SPY") for month in range(1, 13)
    }
    assert set(acquired) == expected
    assert set(released) == expected
    # And ingest_symbols fired exactly once with the gapped symbols.
    assert ingest_calls == [["AAPL", "SPY"]]
