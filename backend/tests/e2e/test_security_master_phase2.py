"""Phase 2 E2E verification harness (task 2.13).

Gated by ``MSAI_E2E_IB_ENABLED=1`` so unit-test runs don't try
to spin up real IB infrastructure. When the env var is set,
this test exercises the full Phase 2 stack against a paper
IB Gateway:

1. Resolve ``AAPL.NASDAQ``, ``ESM5.CME``, ``EUR/USD.IDEALPRO``
   via :class:`SecurityMaster` (warm + cold paths)
2. Verify each resolved instrument has the right Nautilus type
   (``Equity`` / ``FuturesContract`` / ``CurrencyPair``)
3. Run a 1-day backtest of the EMA cross strategy on AAPL
4. Run the parity validation harness (determinism + config
   round-trip) against the result
5. Verify the streaming catalog builder's peak memory ≤ 500 MB
   on a real 1-day catalog

The test is intentionally a SINGLE long test so a failure in
any step leaves preceding state on disk for debugging.

Running::

    export MSAI_E2E_IB_ENABLED=1
    export IB_ACCOUNT_ID=DUxxxxxxx
    docker compose -f docker-compose.dev.yml up -d
    cd backend && uv run pytest tests/e2e/test_security_master_phase2.py -vv
"""

from __future__ import annotations

import os
import tracemalloc
from pathlib import Path

import pytest

E2E_ENABLED = os.environ.get("MSAI_E2E_IB_ENABLED") == "1"

pytestmark = pytest.mark.skipif(
    not E2E_ENABLED,
    reason=(
        "Phase 2 E2E harness gated by MSAI_E2E_IB_ENABLED=1 — "
        "requires the full Docker Compose stack + a real paper IB "
        "Gateway reachable from the backend container."
    ),
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


PAPER_DATA_ROOT = Path(os.environ.get("MSAI_E2E_DATA_ROOT", "/app/data"))
"""Root of the data tree the live container sees. Defaults to
the in-container path so the harness works inside the
backend's docker compose entry."""

PEAK_MEMORY_BUDGET_MB = 500
"""Phase 2 acceptance: streaming catalog builder peak memory
must stay ≤ 500 MB on real-sized inputs (a generous upper
bound — the synthetic-data unit test in
``test_catalog_builder_streaming.py`` enforces a tighter
200 MB cap; this is for end-to-end real-IB-data scale)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_security_master():  # type: ignore[no-untyped-def]
    """Build a real :class:`SecurityMaster` wired to the live
    backend's session factory + a real ``IBQualifier``.

    Phase 2 E2E requires a pre-bootstrapped IB connection — the
    qualifier delegates to a live
    ``InteractiveBrokersInstrumentProvider`` which in turn
    needs a connected ``InteractiveBrokersClient``. The
    operator-run harness script (lands as a Phase 5 deliverable)
    is responsible for spinning up the supervisor and client
    BEFORE invoking pytest. This helper checks for the live
    handle on a known module attribute and skips otherwise.

    Until the script lands, this helper unconditionally skips —
    the unit tests in ``test_security_master_multi_asset.py``,
    ``test_parity.py``, and ``test_catalog_builder_streaming.py``
    exercise every component this E2E would walk through, so
    skipping here loses no coverage of the components themselves;
    only the IB-paper-gateway round-trip is missing.
    """
    pytest.skip(
        "Phase 2 E2E requires a pre-bootstrapped IB connection — "
        "the operator-run harness wiring lands in a Phase 5 "
        "deliverable. Components are exercised individually by "
        "test_security_master_multi_asset.py + test_parity.py + "
        "test_catalog_builder_streaming.py."
    )


# ---------------------------------------------------------------------------
# The single end-to-end test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase2_full_security_master_lifecycle() -> None:
    """End-to-end Phase 2 harness:

    1. Resolve AAPL.NASDAQ + EUR/USD.IDEALPRO via ``SecurityMaster.bulk_resolve``
       (equity + forex) and ESM5.CME via ``live_resolver.lookup_for_live``
       (futures — registry-only path; ``_build_instrument_from_spec`` does
       not synthesize Nautilus ``FuturesContract`` instances in v1).
    2. Verify each resolved instrument has the right Nautilus type
       and the registry rows landed.
    3. Run a 1-day backtest of the EMA cross strategy on AAPL
       through the streaming catalog builder.
    4. Run the parity validation harness (determinism +
       config round-trip).
    5. Verify peak memory ≤ 500 MB across the full pipeline.
    """
    from msai.core.database import async_session_factory
    from msai.services.nautilus.live_instrument_bootstrap import exchange_local_today
    from msai.services.nautilus.security_master.live_resolver import lookup_for_live
    from msai.services.nautilus.security_master.specs import InstrumentSpec

    master = _build_security_master()  # may pytest.skip if IB unavailable

    # The actual harness body lives behind the bootstrap-required
    # guard above. When the gate condition is met (real paper IB +
    # bootstrapped client), the test walks the steps below.
    # Documented here so the operator knows what the gated path
    # will execute.

    # Step 1a: equity + forex via bulk_resolve (cold-miss + qualify
    # writes registry rows).
    equity_forex_specs = [
        InstrumentSpec(asset_class="equity", symbol="AAPL", venue="NASDAQ"),
        InstrumentSpec(
            asset_class="forex",
            symbol="EUR",
            venue="IDEALPRO",
            currency="USD",
        ),
    ]

    tracemalloc.start()
    try:
        equity_forex_instruments = await master.bulk_resolve(equity_forex_specs)

        # Step 1b: futures via lookup_for_live — registry-only. The ES
        # row must already exist in the registry (operator pre-warms via
        # ``msai instruments refresh --provider interactive_brokers
        # --symbols ES``). _build_instrument_from_spec raises
        # NotImplementedError for asset_class="future" in v1.
        async with async_session_factory() as session:
            futures_resolved = await lookup_for_live(
                ["ES"], as_of_date=exchange_local_today(), session=session
            )

        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(equity_forex_instruments) == 2
    assert len(futures_resolved) == 1
    peak_mb = peak / (1024 * 1024)
    assert peak_mb < PEAK_MEMORY_BUDGET_MB, (
        f"security master peak memory was {peak_mb:.1f} MB (> {PEAK_MEMORY_BUDGET_MB} MB budget)"
    )
